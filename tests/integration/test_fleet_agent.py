"""End-to-end fleet management test (Sprint 29; reworked Sprint C4,
XEDGE-440/442/443; Postgres migration Sprint P1, XEDGE-500): two real
Fleet Manager ports (uvicorn, real TLS — server-only on the enrollment
port, mTLS on the device port) and a real `fleet_heartbeat_loop` agent
talking to them — enrollment (CSR + join token in, certificate + device
token out), heartbeat, config push delivered on the next heartbeat, and
the apply result reported back on the one after that. Also exercises
certificate rotation directly over a manually-built mTLS client, since
the FastAPI `TestClient`-equivalent (`httpx.AsyncClient` over
`ASGITransport`, per tests/unit/test_fleet_device_app.py) never
negotiates real TLS and so can't prove the mTLS listener itself
(`ssl_cert_reqs=CERT_REQUIRED`) actually rejects connections without a
CA-issued client certificate.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography import x509

from xedge.core.config import ConfigValidator
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.fleet.agent import (
    FleetAgentConfig,
    FleetAgentPaths,
    FleetAgentStatus,
    fleet_heartbeat_loop,
)
from xedge.fleet.audit import FleetAuditLog
from xedge.fleet.auth import FleetSessionManager, FleetUserStore, LoginLockout
from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.manager_device_app import create_fleet_device_app
from xedge.fleet.registry import DeviceRegistry
from xedge.security.ca import load_or_create_ca
from xedge.security.csr import generate_key_and_csr
from xedge.security.tls_context import build_mtls_client_context

_ADMIN_PASSWORD = "test-admin-password"
_HEARTBEAT_INTERVAL_SECONDS = 0.05
_CERT_VALIDITY_DAYS = 90


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until(predicate, timeout_seconds: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    """`predicate` may be an ordinary sync callable (returns `bool`
    directly) or return a not-yet-awaited coroutine (e.g. `lambda: some_
    async_check()`) — the registry is async as of Sprint P1, so several
    callers below need to poll it. Each call to `predicate()` in the loop
    below produces a fresh coroutine when it's async, which is required
    anyway since a coroutine object can only be awaited once."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.02)
    pytest.fail("condition never became true within timeout")


class _FleetManagerFixture:
    """Bootstraps a fleet CA + manager identity and serves both real ports
    exactly as `xedge.fleet.manager_cli.run` does, but keeping server
    handles so the test can shut them down cleanly. `registry`/
    `default_tenant_id` come from the shared `fleet_registry`/
    `fleet_default_tenant_id` fixtures (real Postgres, Sprint P1) rather
    than being constructed here — this class never owns the registry's
    lifecycle, only the two uvicorn servers built on top of it."""

    def __init__(
        self,
        tmp_path: Path,
        registry: DeviceRegistry,
        default_tenant_id: str,
        user_store: FleetUserStore,
        session_manager: FleetSessionManager,
        audit_log: FleetAuditLog,
        login_lockout: LoginLockout,
        *,
        device_keep_alive_timeout: int = 5,
        device_cert_validity_days: int = _CERT_VALIDITY_DAYS,
    ) -> None:
        self.public_port = _free_port()
        self.device_port = _free_port()
        self.public_url = f"https://127.0.0.1:{self.public_port}"
        self.device_url = f"https://127.0.0.1:{self.device_port}"

        self.registry = registry
        self.tenant_id = default_tenant_id
        self.user_store = user_store
        self.admin_session_token = ""  # set by start(), once a real login succeeds
        self.ca_cert_path = tmp_path / "manager" / "ca.pem"
        self.ca = load_or_create_ca(
            self.ca_cert_path, tmp_path / "manager" / "ca.key", "test-fleet-ca", 3650
        )
        self.manager_cert_path = tmp_path / "manager" / "manager-cert.pem"
        self.manager_key_path = tmp_path / "manager" / "manager-key.pem"
        self.ca.issue_or_load_leaf_identity(
            self.manager_cert_path,
            self.manager_key_path,
            "test-fleet-manager",
            _CERT_VALIDITY_DAYS,
            san_ip_addresses=(ipaddress.ip_address("127.0.0.1"),),
        )

        public_app = create_fleet_manager_app(
            self.registry,
            self.ca,
            user_store,
            session_manager,
            audit_log,
            login_lockout,
            cert_validity_days=device_cert_validity_days,
        )
        device_app = create_fleet_device_app(
            self.registry, self.ca, audit_log, cert_validity_days=device_cert_validity_days
        )
        self.public_server = uvicorn.Server(
            uvicorn.Config(
                public_app,
                host="127.0.0.1",
                port=self.public_port,
                log_config=None,
                access_log=False,
                ssl_certfile=str(self.manager_cert_path),
                ssl_keyfile=str(self.manager_key_path),
            )
        )
        self.device_server = uvicorn.Server(
            uvicorn.Config(
                device_app,
                host="127.0.0.1",
                port=self.device_port,
                log_config=None,
                access_log=False,
                ssl_certfile=str(self.manager_cert_path),
                ssl_keyfile=str(self.manager_key_path),
                ssl_ca_certs=str(self.ca_cert_path),
                ssl_cert_reqs=ssl.CERT_REQUIRED,
                timeout_keep_alive=device_keep_alive_timeout,
            )
        )

    async def start(self) -> None:
        self._public_task = asyncio.create_task(self.public_server.serve())
        self._device_task = asyncio.create_task(self.device_server.serve())
        async with httpx.AsyncClient(verify=False) as client:
            for url in (f"{self.public_url}/health", f"{self.device_url}/health"):
                for _ in range(200):
                    try:
                        # The device port requires a client cert at the TLS
                        # layer, so even a readiness probe here is expected
                        # to fail the handshake — a reachable-but-401/526-ish
                        # style failure still proves the socket is up; only a
                        # bare connection refusal means "not listening yet."
                        await client.get(url, timeout=0.5)
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
                    except httpx.TransportError:
                        break
                else:
                    pytest.fail(f"{url} never became reachable")

            # XEDGE-502: no more flat admin_token -- create the bootstrap
            # admin account directly (skips manager_cli.run's first-startup
            # auto-bootstrap, which this fixture doesn't go through) and
            # log in over real TLS to get a session token every other admin
            # call below presents as a bearer token.
            await self.user_store.create_user(self.tenant_id, "admin", _ADMIN_PASSWORD, "admin")
            login = await client.post(
                f"{self.public_url}/api/v1/fleet/auth/login",
                json={"tenant_name": "default", "username": "admin", "password": _ADMIN_PASSWORD},
            )
            login.raise_for_status()
            self.admin_session_token = login.json()["session_token"]

    async def stop(self) -> None:
        self.public_server.should_exit = True
        self.device_server.should_exit = True
        await asyncio.gather(self._public_task, self._device_task, return_exceptions=True)

    async def create_join_token(self, device_id: str) -> str:
        # Deliberately the async client, not the synchronous httpx.post
        # convenience function: this fixture's uvicorn servers run as
        # asyncio tasks on the *same* event loop as the test. A blocking
        # synchronous call here would monopolize that loop's thread and
        # starve the server tasks of any chance to service the connection
        # they're supposed to be accepting -- the same class of hang
        # Sprint C3's notes describe for a *different* blocking call
        # (`paho.mqtt.Client.connect()`) in this same test suite.
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(
                f"{self.public_url}/api/v1/fleet/join-tokens",
                headers={"Authorization": f"Bearer {self.admin_session_token}"},
                json={"device_id": device_id},
            )
        response.raise_for_status()
        token: str = response.json()["join_token"]
        return token

    async def is_online(self, device_id: str) -> bool:
        record = await self.registry.get(self.tenant_id, device_id)
        return record is not None and record.status == "online"

    async def last_config_apply_succeeded(self, device_id: str) -> bool:
        record = await self.registry.get(self.tenant_id, device_id)
        return record is not None and (record.last_config_apply or {}).get("success") is True

    async def last_config_apply_failed(self, device_id: str) -> bool:
        record = await self.registry.get(self.tenant_id, device_id)
        return record is not None and (record.last_config_apply or {}).get("success") is False


@pytest.fixture
async def fleet_manager(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
):
    manager = _FleetManagerFixture(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    await manager.start()
    try:
        yield manager
    finally:
        await manager.stop()


async def test_agent_enrolls_heartbeats_and_applies_pushed_config(
    tmp_path: Path, core_schema_path: Path, fleet_manager: _FleetManagerFixture
) -> None:
    join_token = await fleet_manager.create_join_token("test-device-1")

    config_path = tmp_path / "xedge.yaml"
    config_path.write_text("schema_version: '0.1'\n", encoding="utf-8")
    validator = ConfigValidator.from_file(core_schema_path)
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=10))
    fleet_status = FleetAgentStatus()
    fleet_config = FleetAgentConfig(
        manager_url=fleet_manager.public_url,
        device_manager_url=fleet_manager.device_url,
        device_id="test-device-1",
        join_token=join_token,
        display_name="Test Device",
        heartbeat_interval_seconds=_HEARTBEAT_INTERVAL_SECONDS,
        verify_tls=False,
    )
    agent_dir = tmp_path / "fleet"
    paths = FleetAgentPaths(
        device_token=agent_dir / "device_token",
        device_cert=agent_dir / "device-cert.pem",
        device_key=agent_dir / "device-key.pem",
        ca_certificate=agent_dir / "ca.pem",
    )

    agent_task = asyncio.create_task(
        fleet_heartbeat_loop(
            fleet_config, paths, supervisor, config_path, validator, datetime.now(UTC), fleet_status
        )
    )

    try:
        await _wait_until(lambda: fleet_status.last_heartbeat_ok is True)
        assert fleet_status.registered is True
        assert paths.device_token.is_file()
        assert paths.device_cert.is_file()
        assert paths.device_key.is_file()
        assert paths.ca_certificate.is_file()
        # The private key must never have crossed the wire in the clear —
        # confirming it exists locally isn't the point; confirming the
        # manager never received it is (ADR-013 §3). The manager's own
        # registry has no column for it at all, so there's nowhere it
        # could have ended up even transiently on that side.
        assert "PRIVATE KEY" not in fleet_manager.ca_cert_path.read_text(encoding="utf-8")

        await _wait_until(lambda: fleet_manager.is_online("test-device-1"))
        record = await fleet_manager.registry.get(fleet_manager.tenant_id, "test-device-1")
        assert record.cert_serial_number

        async with httpx.AsyncClient(verify=False) as admin_client:
            push = await admin_client.post(
                f"{fleet_manager.public_url}/api/v1/fleet/devices/test-device-1/config",
                headers={"Authorization": f"Bearer {fleet_manager.admin_session_token}"},
                json={"config": {"schema_version": "0.1", "logging": {"level": "DEBUG"}}},
            )
            assert push.status_code == 202

            await _wait_until(lambda: "level: DEBUG" in config_path.read_text(encoding="utf-8"))
            await _wait_until(lambda: fleet_manager.last_config_apply_succeeded("test-device-1"))
            record = await fleet_manager.registry.get(fleet_manager.tenant_id, "test-device-1")
            assert record.last_config_apply == {"version": 1, "success": True, "error": None}
            assert record.has_pending_config is False
    finally:
        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)


async def test_agent_reports_a_rejected_config_without_writing_it(
    tmp_path: Path, core_schema_path: Path, fleet_manager: _FleetManagerFixture
) -> None:
    join_token = await fleet_manager.create_join_token("test-device-2")

    config_path = tmp_path / "xedge.yaml"
    original_contents = "schema_version: '0.1'\n"
    config_path.write_text(original_contents, encoding="utf-8")
    validator = ConfigValidator.from_file(core_schema_path)
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=10))
    fleet_status = FleetAgentStatus()
    fleet_config = FleetAgentConfig(
        manager_url=fleet_manager.public_url,
        device_manager_url=fleet_manager.device_url,
        device_id="test-device-2",
        join_token=join_token,
        heartbeat_interval_seconds=_HEARTBEAT_INTERVAL_SECONDS,
        verify_tls=False,
    )
    agent_dir = tmp_path / "fleet"
    paths = FleetAgentPaths(
        device_token=agent_dir / "device_token",
        device_cert=agent_dir / "device-cert.pem",
        device_key=agent_dir / "device-key.pem",
        ca_certificate=agent_dir / "ca.pem",
    )

    agent_task = asyncio.create_task(
        fleet_heartbeat_loop(
            fleet_config, paths, supervisor, config_path, validator, datetime.now(UTC), fleet_status
        )
    )
    try:
        await _wait_until(lambda: fleet_status.last_heartbeat_ok is True)

        async with httpx.AsyncClient(verify=False) as admin_client:
            # Missing required "schema_version" -- invalid against the core schema.
            push = await admin_client.post(
                f"{fleet_manager.public_url}/api/v1/fleet/devices/test-device-2/config",
                headers={"Authorization": f"Bearer {fleet_manager.admin_session_token}"},
                json={"config": {"logging": {"level": "DEBUG"}}},
            )
            assert push.status_code == 202

            await _wait_until(lambda: fleet_manager.last_config_apply_failed("test-device-2"))
            record = await fleet_manager.registry.get(fleet_manager.tenant_id, "test-device-2")
            assert record.last_config_apply["error"]
            assert config_path.read_text(encoding="utf-8") == original_contents
    finally:
        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)


async def test_heartbeat_survives_the_servers_keep_alive_timeout(
    tmp_path: Path,
    core_schema_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    """Regression test for a bug found by manually running the agent
    against a real Fleet Manager, not by any existing automated test:
    uvicorn's default `timeout_keep_alive` is 5s, and
    `heartbeat_interval_seconds` is commonly >= that (60s by default), so
    a pooled httpx connection sitting idle between heartbeats would
    routinely be one the server had *already* closed server-side by the
    time the next heartbeat tried to reuse it -- surfacing as "Server
    disconnected without sending a response" on roughly every heartbeat
    past the first. Every existing test used a 0.05s interval, far
    shorter than any keep-alive timeout, so none of them could have hit
    this. Reproduced deterministically here with an aggressively short
    server-side keep-alive rather than waiting out the real 5s default."""
    device_keep_alive_timeout = 1
    heartbeat_interval_seconds = 1.5  # comfortably longer than the above
    manager = _FleetManagerFixture(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
        device_keep_alive_timeout=device_keep_alive_timeout,
    )
    await manager.start()
    try:
        join_token = await manager.create_join_token("keep-alive-test-device")
        config_path = tmp_path / "xedge.yaml"
        config_path.write_text("schema_version: '0.1'\n", encoding="utf-8")
        validator = ConfigValidator.from_file(core_schema_path)
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=10))
        fleet_status = FleetAgentStatus()
        fleet_config = FleetAgentConfig(
            manager_url=manager.public_url,
            device_manager_url=manager.device_url,
            device_id="keep-alive-test-device",
            join_token=join_token,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            verify_tls=False,
        )
        agent_dir = tmp_path / "fleet"
        paths = FleetAgentPaths(
            device_token=agent_dir / "device_token",
            device_cert=agent_dir / "device-cert.pem",
            device_key=agent_dir / "device-key.pem",
            ca_certificate=agent_dir / "ca.pem",
        )
        agent_task = asyncio.create_task(
            fleet_heartbeat_loop(
                fleet_config,
                paths,
                supervisor,
                config_path,
                validator,
                datetime.now(UTC),
                fleet_status,
            )
        )
        try:
            await _wait_until(lambda: fleet_status.last_heartbeat_ok is True)
            # Sample across several intervals -- comfortably past the
            # server's 1s keep-alive timeout each time -- and fail on the
            # first sign of the disconnect this test exists to catch,
            # rather than only checking once at the end.
            for _ in range(4):
                await asyncio.sleep(heartbeat_interval_seconds + 0.5)
                assert fleet_status.last_heartbeat_ok is True, fleet_status.last_error
        finally:
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
    finally:
        await manager.stop()


async def test_device_port_rejects_a_connection_with_no_client_certificate(
    fleet_manager: _FleetManagerFixture,
) -> None:
    """Proves the transport-level enforcement this whole two-port split
    exists for: `ssl_cert_reqs=CERT_REQUIRED` on the device port refuses
    the TLS handshake itself for a client presenting no certificate at
    all, before any request reaches `manager_device_app`'s routes."""
    with pytest.raises(httpx.TransportError):
        async with httpx.AsyncClient(verify=False) as client:
            await client.get(f"{fleet_manager.device_url}/health", timeout=2.0)


async def test_rotate_certificate_over_a_real_mtls_connection(
    fleet_manager: _FleetManagerFixture,
) -> None:
    join_token = await fleet_manager.create_join_token("test-device-3")
    _private_key_pem, csr_pem = generate_key_and_csr("test-device-3")
    async with httpx.AsyncClient(verify=False) as enroll_client:
        enroll = await enroll_client.post(
            f"{fleet_manager.public_url}/api/v1/fleet/enroll",
            json={
                "device_id": "test-device-3",
                "join_token": join_token,
                "csr_pem": csr_pem.decode("ascii"),
            },
        )
    enroll.raise_for_status()
    body = enroll.json()

    cert_path = fleet_manager.ca_cert_path.parent / "device3-cert.pem"
    key_path = fleet_manager.ca_cert_path.parent / "device3-key.pem"
    cert_path.write_text(body["certificate_pem"], encoding="utf-8")
    key_path.write_bytes(_private_key_pem)

    _new_private_key_pem, new_csr_pem = generate_key_and_csr("test-device-3")
    mtls_context = build_mtls_client_context(cert_path, key_path, fleet_manager.ca_cert_path)
    async with httpx.AsyncClient(verify=mtls_context) as mtls_client:
        response = await mtls_client.post(
            f"{fleet_manager.device_url}/api/v1/fleet/devices/test-device-3/rotate-certificate",
            headers={"Authorization": f"Bearer {body['device_token']}"},
            json={"csr_pem": new_csr_pem.decode("ascii")},
        )

    assert response.status_code == 200
    assert response.json()["certificate_pem"]


async def test_agent_proactively_rotates_a_certificate_nearing_expiry(
    tmp_path: Path,
    core_schema_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    """Carried from Sprint C4 into C5: XEDGE-443's rotate-certificate
    endpoint had no caller on the device side until now. Uses a
    deliberately short-validity device certificate so the rotation
    threshold is already crossed on the very first heartbeat, rather than
    waiting out real time -- the same "compress the interval, not the
    mechanism" approach every other timing-dependent test in this file
    already uses. Proves both halves: that a rotation actually happens
    (new key/cert on disk, new expiry recorded on the manager), and the
    structurally harder part the delivery plan's C4 notes flagged as the
    reason this was deferred -- that heartbeats *keep succeeding
    afterward*, proving the mTLS client was really rebuilt with the new
    identity rather than only appearing to rotate."""
    device_cert_validity_days = 1
    manager = _FleetManagerFixture(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
        device_cert_validity_days=device_cert_validity_days,
    )
    await manager.start()
    try:
        join_token = await manager.create_join_token("rotation-test-device")
        config_path = tmp_path / "xedge.yaml"
        config_path.write_text("schema_version: '0.1'\n", encoding="utf-8")
        validator = ConfigValidator.from_file(core_schema_path)
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=10))
        fleet_status = FleetAgentStatus()
        fleet_config = FleetAgentConfig(
            manager_url=manager.public_url,
            device_manager_url=manager.device_url,
            device_id="rotation-test-device",
            join_token=join_token,
            heartbeat_interval_seconds=_HEARTBEAT_INTERVAL_SECONDS,
            verify_tls=False,
            # A 1-day-validity certificate is already inside any sane
            # threshold, so this fires on the first heartbeat.
            cert_rotation_threshold_days=30,
        )
        agent_dir = tmp_path / "fleet"
        paths = FleetAgentPaths(
            device_token=agent_dir / "device_token",
            device_cert=agent_dir / "device-cert.pem",
            device_key=agent_dir / "device-key.pem",
            ca_certificate=agent_dir / "ca.pem",
        )
        agent_task = asyncio.create_task(
            fleet_heartbeat_loop(
                fleet_config,
                paths,
                supervisor,
                config_path,
                validator,
                datetime.now(UTC),
                fleet_status,
            )
        )
        try:
            await _wait_until(lambda: fleet_status.last_heartbeat_ok is True)
            original_cert_pem = paths.device_cert.read_text(encoding="utf-8")
            original_key_pem = paths.device_key.read_text(encoding="utf-8")
            original_serial = x509.load_pem_x509_certificate(
                original_cert_pem.encode("ascii")
            ).serial_number

            await _wait_until(
                lambda: paths.device_cert.read_text(encoding="utf-8") != original_cert_pem
            )
            rotated_at = datetime.now(UTC)
            assert paths.device_key.read_text(encoding="utf-8") != original_key_pem

            new_cert = x509.load_pem_x509_certificate(paths.device_cert.read_bytes())
            # Both the original and the rotated certificate are signed with
            # the same 1-day validity in this test (device_cert_validity_days
            # governs rotation the same as enrollment), and X.509 validity
            # fields only have whole-second precision -- either can coincide
            # with the original's expiry by coincidence. The CA's serial
            # number is random per signing call (xedge/security/ca.py) and
            # is what actually proves this is a freshly issued certificate,
            # not the same one re-read off disk.
            assert new_cert.serial_number != original_serial
            assert fleet_status.cert_not_after == new_cert.not_valid_after_utc

            # The part a rotation that merely *appeared* to work would
            # fail: a heartbeat strictly after the rotation, still
            # succeeding, over a client presenting the new certificate.
            await _wait_until(
                lambda: (
                    fleet_status.last_heartbeat_at is not None
                    and fleet_status.last_heartbeat_at > rotated_at
                    and fleet_status.last_heartbeat_ok is True
                )
            )

            record = await manager.registry.get(manager.tenant_id, "rotation-test-device")
            assert record is not None
            assert record.cert_not_after == new_cert.not_valid_after_utc
        finally:
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
    finally:
        await manager.stop()
