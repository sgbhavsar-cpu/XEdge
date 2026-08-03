from __future__ import annotations

from pathlib import Path

from cryptography import x509
from httpx import ASGITransport, AsyncClient, Response

from xedge.core.config import ConfigValidator
from xedge.fleet.audit import FleetAuditLog
from xedge.fleet.auth import FleetSessionManager, FleetUserStore, LoginLockout
from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.registry import DEFAULT_TENANT_NAME, DeviceRegistry
from xedge.security.ca import CertificateAuthority, load_or_create_ca
from xedge.security.csr import generate_key_and_csr

_CERT_VALIDITY_DAYS = 90
_ADMIN_PASSWORD = "correct horse battery staple"


def _client(
    tmp_path: Path,
    registry: DeviceRegistry,
    user_store: FleetUserStore,
    session_manager: FleetSessionManager,
    audit_log: FleetAuditLog,
    login_lockout: LoginLockout,
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
        user_store,
        session_manager,
        audit_log,
        login_lockout,
        cert_validity_days=_CERT_VALIDITY_DAYS,
        config_validator=config_validator,
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, ca


async def _create_admin(
    user_store: FleetUserStore, tenant_id: str, username: str = "admin"
) -> None:
    await user_store.create_user(tenant_id, username, _ADMIN_PASSWORD, "admin")


async def _login(
    client: AsyncClient,
    tenant_name: str = DEFAULT_TENANT_NAME,
    username: str = "admin",
    password: str = _ADMIN_PASSWORD,
) -> Response:
    return await client.post(
        "/api/v1/fleet/auth/login",
        json={"tenant_name": tenant_name, "username": username, "password": password},
    )


def _auth_headers(session_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {session_token}"}


async def _admin_session_headers(
    client: AsyncClient, user_store: FleetUserStore, tenant_id: str
) -> dict[str, str]:
    """Convenience for the many tests that just need *a* valid admin
    session and don't care about the login flow itself."""
    await _create_admin(user_store, tenant_id)
    response = await _login(client)
    assert response.status_code == 200
    return _auth_headers(response.json()["session_token"])


async def _provision_join_token(
    client: AsyncClient, device_id: str, headers: dict[str, str], ttl_seconds: float = 3600
) -> str:
    response = await client.post(
        "/api/v1/fleet/join-tokens",
        headers=headers,
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


# --- auth: login ---


async def test_login_with_valid_credentials_returns_a_session_token(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await _create_admin(fleet_user_store, fleet_default_tenant_id)

    response = await _login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["session_token"]
    assert body["username"] == "admin"
    assert body["role"] == "admin"


async def test_login_with_wrong_password_is_401(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await _create_admin(fleet_user_store, fleet_default_tenant_id)

    response = await _login(client, password="wrong")

    assert response.status_code == 401


async def test_login_with_unknown_tenant_name_is_401(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    """Same 401 as a wrong password -- an unknown tenant name isn't
    distinguishable from a wrong password, the same anti-enumeration
    posture used everywhere else in this API."""
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await _create_admin(fleet_user_store, fleet_default_tenant_id)

    response = await _login(client, tenant_name="no-such-tenant")

    assert response.status_code == 401


async def test_login_is_locked_out_after_repeated_failures(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await _create_admin(fleet_user_store, fleet_default_tenant_id)

    for _ in range(5):
        assert (await _login(client, password="wrong")).status_code == 401

    locked = await _login(client, password="wrong")
    assert locked.status_code == 429
    # Even the *correct* password is refused while locked out.
    still_locked = await _login(client)
    assert still_locked.status_code == 429


async def test_session_token_authenticates_subsequent_requests(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    response = await client.get("/api/v1/fleet/devices", headers=headers)

    assert response.status_code == 200


async def test_missing_session_token_is_401(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )

    assert (await client.get("/api/v1/fleet/devices")).status_code == 401
    wrong = await client.get(
        "/api/v1/fleet/devices", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert wrong.status_code == 401


# --- RBAC: role-appropriate permissions ---


async def test_readonly_role_cannot_create_join_tokens(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await fleet_user_store.create_user(fleet_default_tenant_id, "viewer", "pw", "readonly")
    login = await _login(client, username="viewer", password="pw")
    headers = _auth_headers(login.json()["session_token"])

    response = await client.post(
        "/api/v1/fleet/join-tokens", headers=headers, json={"device_id": "dev1"}
    )

    assert response.status_code == 403


async def test_readonly_role_can_list_devices(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await fleet_user_store.create_user(fleet_default_tenant_id, "viewer", "pw", "readonly")
    login = await _login(client, username="viewer", password="pw")
    headers = _auth_headers(login.json()["session_token"])

    response = await client.get("/api/v1/fleet/devices", headers=headers)

    assert response.status_code == 200


async def test_operator_role_cannot_manage_users(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await fleet_user_store.create_user(fleet_default_tenant_id, "op", "pw", "operator")
    login = await _login(client, username="op", password="pw")
    headers = _auth_headers(login.json()["session_token"])

    response = await client.get("/api/v1/fleet/users", headers=headers)

    assert response.status_code == 403


async def test_auditor_role_can_read_audit_log_but_not_write_config(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await fleet_user_store.create_user(fleet_default_tenant_id, "aud", "pw", "auditor")
    login = await _login(client, username="aud", password="pw")
    headers = _auth_headers(login.json()["session_token"])

    assert (await client.get("/api/v1/fleet/audit", headers=headers)).status_code == 200
    denied = await client.post(
        "/api/v1/fleet/join-tokens", headers=headers, json={"device_id": "dev1"}
    )
    assert denied.status_code == 403


# --- user management ---


async def test_create_user_requires_user_manage_permission(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    response = await client.post(
        "/api/v1/fleet/users",
        headers=headers,
        json={"username": "bob", "password": "pw", "role": "operator"},
    )

    assert response.status_code == 201
    assert response.json() == {"username": "bob", "role": "operator"}


async def test_create_user_with_duplicate_username_is_409(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    response = await client.post(
        "/api/v1/fleet/users",
        headers=headers,
        json={"username": "admin", "password": "pw", "role": "operator"},
    )

    assert response.status_code == 409


async def test_list_users_returns_usernames_and_roles_only(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    await fleet_user_store.create_user(fleet_default_tenant_id, "bob", "pw", "operator")

    response = await client.get("/api/v1/fleet/users", headers=headers)

    assert response.status_code == 200
    users = {u["username"]: u["role"] for u in response.json()}
    assert users == {"admin": "admin", "bob": "operator"}
    assert all("password" not in u and "password_hash" not in u for u in response.json())


async def test_update_user_role_takes_effect_on_existing_session(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    admin_headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    await fleet_user_store.create_user(fleet_default_tenant_id, "bob", "pw", "operator")
    bob_login = await _login(client, username="bob", password="pw")
    bob_headers = _auth_headers(bob_login.json()["session_token"])
    assert (await client.get("/api/v1/fleet/users", headers=bob_headers)).status_code == 403

    demote = await client.patch(
        "/api/v1/fleet/users/bob", headers=admin_headers, json={"role": "admin"}
    )
    assert demote.status_code == 200

    # bob's *existing* session immediately gains the new role -- role is
    # looked up fresh per request, never cached on the session token.
    assert (await client.get("/api/v1/fleet/users", headers=bob_headers)).status_code == 200


async def test_delete_last_admin_is_409(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    response = await client.delete("/api/v1/fleet/users/admin", headers=headers)

    assert response.status_code == 409


async def test_delete_unknown_user_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    response = await client.delete("/api/v1/fleet/users/nope", headers=headers)

    assert response.status_code == 404


# --- audit log ---


async def test_admin_actions_are_recorded_in_the_audit_log(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    await client.post(
        "/api/v1/fleet/users",
        headers=headers,
        json={"username": "bob", "password": "pw", "role": "operator"},
    )
    await _provision_join_token(client, "dev1", headers)

    response = await client.get("/api/v1/fleet/audit", headers=headers)

    assert response.status_code == 200
    events = [e["event"] for e in response.json()]
    assert "auth.login_success" in events
    assert "user.created" in events
    assert "join_token.created" in events


async def test_audit_log_event_prefix_filter(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    await client.post(
        "/api/v1/fleet/users",
        headers=headers,
        json={"username": "bob", "password": "pw", "role": "operator"},
    )

    response = await client.get("/api/v1/fleet/audit", headers=headers, params={"event": "user."})

    assert response.status_code == 200
    assert all(e["event"].startswith("user.") for e in response.json())
    assert len(response.json()) == 1


# --- join tokens / enrollment / devices (unchanged behavior, new auth) ---


async def test_enroll_with_valid_join_token_returns_certificate_and_device_token(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)

    response = await _enroll(client, "dev1", join_token)

    assert response.status_code == 200
    body = response.json()
    assert body["device_token"]
    assert body["ca_certificate_pem"] == ca.certificate_pem.decode("ascii")
    certificate = x509.load_pem_x509_certificate(body["certificate_pem"].encode("ascii"))
    assert certificate.subject.rfc4514_string() == "CN=dev1"
    assert certificate.issuer == ca.certificate.subject


async def test_enroll_records_certificate_status_on_the_device_record(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record is not None
    assert record.cert_serial_number
    assert record.cert_not_after is not None


async def test_enroll_with_invalid_join_token_is_rejected(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    response = await _enroll(client, "dev1", "not-a-real-token")
    assert response.status_code == 401


async def test_enroll_join_token_is_single_use(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)

    first = await _enroll(client, "dev1", join_token)
    second = await _enroll(client, "dev1", join_token)

    assert first.status_code == 200
    assert second.status_code == 401


async def test_enroll_rejects_a_join_token_provisioned_for_a_different_device(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)

    response = await _enroll(client, "dev2", join_token)

    assert response.status_code == 401


async def test_list_and_get_devices(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)

    ok = await client.get("/api/v1/fleet/devices", headers=headers)
    assert ok.status_code == 200
    assert [d["device_id"] for d in ok.json()] == ["dev1"]

    detail = await client.get("/api/v1/fleet/devices/dev1", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "unknown"
    assert detail.json()["connection_state"] == "inactive"
    assert detail.json()["cert_serial_number"]


async def test_get_unknown_device_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    response = await client.get("/api/v1/fleet/devices/nope", headers=headers)
    assert response.status_code == 404


async def test_update_metadata_sets_only_the_provided_fields(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)

    response = await client.patch(
        "/api/v1/fleet/devices/dev1/metadata",
        headers=headers,
        json={"serial_number": "SN-123", "make": "Acme Gateways"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["serial_number"] == "SN-123"
    assert body["make"] == "Acme Gateways"
    assert body["protocol"] is None


async def test_update_metadata_for_unknown_device_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    response = await client.patch(
        "/api/v1/fleet/devices/nope/metadata", headers=headers, json={"make": "Acme"}
    )
    assert response.status_code == 404


async def test_config_push_queues_for_an_enrolled_device(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)

    push = await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=headers,
        json={"config": {"schema_version": "0.1"}},
    )
    assert push.status_code == 202
    assert push.json() == {"queued": True, "pending_config_version": 1}

    status_response = await client.get("/api/v1/fleet/devices/dev1/config/status", headers=headers)
    assert status_response.json()["has_pending_config"] is True


async def test_config_push_to_unknown_device_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    response = await client.post(
        "/api/v1/fleet/devices/nope/config", headers=headers, json={"config": {}}
    )
    assert response.status_code == 404


async def test_config_push_without_a_validator_configured_skips_validation(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    """`config_validator=None` (this fixture's default) is a real supported
    mode, not just a test convenience -- confirms an invalid config is
    still queued rather than raising, since no validator was supplied."""
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)

    push = await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=headers,
        json={"config": {"this_is_not_a_valid_xedge_config": True}},
    )
    assert push.status_code == 202


async def test_config_push_with_a_validator_rejects_an_invalid_config(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
    core_schema_path: Path,
) -> None:
    """XEDGE-446: an operator authoring a config finds out immediately,
    not only after the device reports a failed apply on its next
    heartbeat."""
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
        config_validator=ConfigValidator.from_file(core_schema_path),
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)

    push = await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=headers,
        json={"config": {"this_is_not_a_valid_xedge_config": True}},
    )

    assert push.status_code == 400
    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.has_pending_config is False


async def test_config_push_with_a_validator_accepts_a_valid_config(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
    core_schema_path: Path,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
        config_validator=ConfigValidator.from_file(core_schema_path),
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)

    push = await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=headers,
        json={"config": {"schema_version": "0.1"}},
    )

    assert push.status_code == 202


async def test_create_join_token_for_a_device_owned_by_another_tenant_is_409(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    other_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    """XEDGE-504 surfaced through the API: `DeviceTenantConflictError`
    from the registry becomes a 409, not a 500."""
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    # The conflict guard is on `devices`, not `join_tokens` -- "dev1" has
    # to actually be *registered* under tenant A (via a completed enroll)
    # for tenant B's attempt below to find anything to conflict with.
    await _enroll(client, "dev1", join_token)

    await fleet_user_store.create_user(other_tenant_id, "admin", _ADMIN_PASSWORD, "admin")
    other_login = await _login(client, tenant_name="other-tenant")
    other_headers = _auth_headers(other_login.json()["session_token"])

    response = await client.post(
        "/api/v1/fleet/join-tokens", headers=other_headers, json={"device_id": "dev1"}
    )

    assert response.status_code == 409


async def test_two_tenants_do_not_see_each_others_devices_through_the_api(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    other_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    """End-to-end version of XEDGE-503's isolation requirement: two
    tenants sharing one registry, apps, and login endpoint must not see
    each other's devices or audit history through real HTTP calls."""
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    tenant_a_headers = await _admin_session_headers(
        client, fleet_user_store, fleet_default_tenant_id
    )
    join_token = await _provision_join_token(client, "dev1", tenant_a_headers)
    await _enroll(client, "dev1", join_token)

    await fleet_user_store.create_user(other_tenant_id, "admin", _ADMIN_PASSWORD, "admin")
    tenant_b_login = await _login(client, tenant_name="other-tenant")
    tenant_b_headers = _auth_headers(tenant_b_login.json()["session_token"])

    tenant_a_devices = (await client.get("/api/v1/fleet/devices", headers=tenant_a_headers)).json()
    tenant_b_devices = (await client.get("/api/v1/fleet/devices", headers=tenant_b_headers)).json()

    assert [d["device_id"] for d in tenant_a_devices] == ["dev1"]
    assert tenant_b_devices == []
    tenant_b_detail = await client.get("/api/v1/fleet/devices/dev1", headers=tenant_b_headers)
    assert tenant_b_detail.status_code == 404

    tenant_a_audit = (await client.get("/api/v1/fleet/audit", headers=tenant_a_headers)).json()
    tenant_b_audit = (await client.get("/api/v1/fleet/audit", headers=tenant_b_headers)).json()
    assert any(e["event"] == "join_token.created" for e in tenant_a_audit)
    assert not any(e["event"] == "join_token.created" for e in tenant_b_audit)


# --- auth: logout (Sprint P3) ---


async def test_logout_invalidates_the_session(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    logout = await client.post("/api/v1/fleet/auth/logout", headers=headers)
    assert logout.status_code == 204

    response = await client.get("/api/v1/fleet/devices", headers=headers)
    assert response.status_code == 401

    entries = await fleet_audit_log.tail(fleet_default_tenant_id, event_prefix="auth.logout")
    assert len(entries) == 1
    assert entries[0].actor == "admin"


async def test_logout_without_a_session_is_401(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )

    response = await client.post("/api/v1/fleet/auth/logout")

    assert response.status_code == 401


# --- join-token list/revoke (Sprint P3, XEDGE-513) ---


async def test_list_join_tokens_shows_status_for_each_token(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    await _provision_join_token(client, "dev1", headers)

    response = await client.get("/api/v1/fleet/join-tokens", headers=headers)

    assert response.status_code == 200
    tokens = response.json()
    assert len(tokens) == 1
    assert tokens[0]["device_id"] == "dev1"
    assert tokens[0]["status"] == "active"
    assert "id" in tokens[0]


async def test_readonly_role_cannot_list_join_tokens(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await fleet_user_store.create_user(fleet_default_tenant_id, "viewer", "pw", "readonly")
    login = await _login(client, username="viewer", password="pw")
    headers = _auth_headers(login.json()["session_token"])

    response = await client.get("/api/v1/fleet/join-tokens", headers=headers)

    assert response.status_code == 403


async def test_revoke_join_token_prevents_enrollment(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    token_id = (await client.get("/api/v1/fleet/join-tokens", headers=headers)).json()[0]["id"]

    revoke = await client.delete(f"/api/v1/fleet/join-tokens/{token_id}", headers=headers)
    assert revoke.status_code == 204

    enroll = await _enroll(client, "dev1", join_token)
    assert enroll.status_code == 401

    audit = (await client.get("/api/v1/fleet/audit", headers=headers)).json()
    assert any(e["event"] == "join_token.revoked" for e in audit)


async def test_revoke_unknown_join_token_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    response = await client.delete("/api/v1/fleet/join-tokens/not-a-real-id", headers=headers)

    assert response.status_code == 404


async def test_revoke_join_token_for_a_token_in_another_tenant_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    other_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    """XEDGE-503: an operator in tenant B must not be able to revoke a
    token that belongs to tenant A."""
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    tenant_a_headers = await _admin_session_headers(
        client, fleet_user_store, fleet_default_tenant_id
    )
    await _provision_join_token(client, "dev1", tenant_a_headers)
    token_id = (await client.get("/api/v1/fleet/join-tokens", headers=tenant_a_headers)).json()[0][
        "id"
    ]

    await fleet_user_store.create_user(other_tenant_id, "admin", _ADMIN_PASSWORD, "admin")
    tenant_b_login = await _login(client, tenant_name="other-tenant")
    tenant_b_headers = _auth_headers(tenant_b_login.json()["session_token"])

    response = await client.delete(
        f"/api/v1/fleet/join-tokens/{token_id}", headers=tenant_b_headers
    )

    assert response.status_code == 404


# --- device config-history / certificate-history (Sprint P3, XEDGE-512) ---


async def test_config_history_reflects_a_push_and_its_later_apply_report(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)
    await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=headers,
        json={"config": {"schema_version": "0.1"}},
    )

    before_apply = await client.get("/api/v1/fleet/devices/dev1/config-history", headers=headers)
    assert before_apply.status_code == 200
    assert len(before_apply.json()) == 1
    assert before_apply.json()[0]["applied_at"] is None
    assert before_apply.json()[0]["pushed_by"] == "admin"

    await fleet_registry.heartbeat(
        "dev1", None, None, None, {"version": 1, "success": True, "error": None}
    )

    after_apply = await client.get("/api/v1/fleet/devices/dev1/config-history", headers=headers)
    assert after_apply.json()[0]["applied_at"] is not None
    assert after_apply.json()[0]["apply_success"] is True


async def test_config_history_for_unknown_device_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    response = await client.get("/api/v1/fleet/devices/nope/config-history", headers=headers)

    assert response.status_code == 404


async def test_config_history_for_a_device_in_another_tenant_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    other_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    """XEDGE-503: every new query path needs a cross-tenant-leak test."""
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    tenant_a_headers = await _admin_session_headers(
        client, fleet_user_store, fleet_default_tenant_id
    )
    join_token = await _provision_join_token(client, "dev1", tenant_a_headers)
    await _enroll(client, "dev1", join_token)

    await fleet_user_store.create_user(other_tenant_id, "admin", _ADMIN_PASSWORD, "admin")
    tenant_b_login = await _login(client, tenant_name="other-tenant")
    tenant_b_headers = _auth_headers(tenant_b_login.json()["session_token"])

    response = await client.get(
        "/api/v1/fleet/devices/dev1/config-history", headers=tenant_b_headers
    )

    assert response.status_code == 404


async def test_certificate_history_shows_the_enrollment_issuance(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1", headers)
    await _enroll(client, "dev1", join_token)

    response = await client.get("/api/v1/fleet/devices/dev1/certificate-history", headers=headers)

    assert response.status_code == 200
    history = response.json()
    assert len(history) == 1
    assert history[0]["reason"] == "enrollment"


async def test_certificate_history_for_unknown_device_is_404(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    headers = await _admin_session_headers(client, fleet_user_store, fleet_default_tenant_id)

    response = await client.get("/api/v1/fleet/devices/nope/certificate-history", headers=headers)

    assert response.status_code == 404
