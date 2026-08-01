"""`xedge-fleet-manager`: standalone entrypoint for the Fleet Manager
service (Sprint 29, XEDGE-211/215; reworked Sprint C4,
XEDGE-440/442/443/446) — a separate process/container from any individual
xEdge device, per Sprint 32's documented split.

Serves **two ports**, not one (ADR-013 §3/§4):

- `--port` (default 8090): `xedge.fleet.manager_app` — join-token
  provisioning, device enrollment, and admin calls. Server-TLS only; an
  un-enrolled device has no client certificate yet to present, and neither
  does an operator/CLI.
- `--device-port` (default 8091): `xedge.fleet.manager_device_app` —
  heartbeat and certificate rotation, for devices that have already
  enrolled. `ssl_cert_reqs=CERT_REQUIRED` against the fleet CA: the TLS
  handshake itself refuses any connection not holding a certificate this
  CA issued. This can't be the same port as the one above — TLS client-
  certificate enforcement applies to the whole listening socket, before
  any request routing happens, so mixing "requires a cert" traffic with
  "has no cert yet" traffic on one port isn't possible.

Both ports serve over the same certificate: a leaf identity this manager's
own fleet CA issues to itself (`xedge.security.ca.CertificateAuthority.
issue_or_load_leaf_identity`), so a device that received the CA chain
during enrollment can verify the manager's identity on the device port
using that same chain.

There is no `--admin-token` anymore (Sprint P2, XEDGE-502 removes it
outright rather than keeping it alongside real accounts — pre-GA, no
deployment depends on it yet). On first-ever startup (no user account
exists for the bootstrapped default tenant), one is created for you --
`username="admin"`, an auto-generated password persisted to
`--data-dir/admin_bootstrap_password` (same load-or-create ergonomics
`--admin-token` used to have: a fresh deployment needs nothing supplied
up front, retrieved the same way — `docker exec ... cat
/data/admin_bootstrap_password`). Log in with it via `POST /api/v1/
fleet/auth/login` to get a session token, then change the password.
There is no `--join-token` anymore either — join tokens are single-use
and per-device now (XEDGE-442), provisioned through the admin API, not
a CLI flag.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import secrets
import ssl
import sys
from importlib import resources
from pathlib import Path

import uvicorn

from xedge import __version__
from xedge.core.config import ConfigValidator
from xedge.fleet.audit import FleetAuditLog
from xedge.fleet.auth import FleetSessionManager, FleetUserStore, LoginLockout
from xedge.fleet.db_models import create_engine
from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.manager_device_app import create_fleet_device_app
from xedge.fleet.migrate import run_migrations
from xedge.fleet.registry import DeviceRegistry
from xedge.security.ca import CertificateAuthority, load_or_create_ca

_CA_COMMON_NAME = "xedge-fleet-ca"
_MANAGER_IDENTITY_COMMON_NAME = "xedge-fleet-manager"
_SCHEMA_FILENAME = "xedge-core.schema.json"
_DEFAULT_DATABASE_URL = "postgresql+asyncpg://xedge:xedge@localhost:5432/xedge_fleet"


def _default_schema_path() -> Path:
    """Same dual-location resolution as `xedge.core.main._default_schema_path`
    (duplicated rather than imported: importing `xedge.core.main` itself
    would pull in the full driver/supervisor composition root this module
    otherwise avoids — see the module docstring). Packaged copy first
    (`xedge/schema/...`, via `[tool.hatch.build.targets.wheel.
    force-include]` — present after a real `pip install`); falls back to
    the repo-root `config/schema/` location for `pip install -e .`."""
    packaged = resources.files("xedge") / "schema" / _SCHEMA_FILENAME
    if packaged.is_file():
        return Path(str(packaged))
    return Path(__file__).resolve().parent.parent.parent / "config" / "schema" / _SCHEMA_FILENAME


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="xedge-fleet-manager", description="xEdge Fleet Manager service"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("/data/fleet-manager"),
        help="Directory for the fleet CA and the first-startup bootstrap admin password "
        "(Sprint P1, XEDGE-500: no longer holds the device registry database — see --database-url)",
    )
    parser.add_argument(
        "--database-url",
        default=_DEFAULT_DATABASE_URL,
        help="Async SQLAlchemy URL for the device registry's Postgres database (Sprint P1, "
        "XEDGE-500 — supersedes the pre-P1 SQLite devices.db under --data-dir). Matches "
        "deploy/docker/docker-compose.fleet.yml's postgres service by default.",
    )
    # nosec B104 — a fleet manager must be reachable from every enrolled
    # device's network, unlike the per-device loopback-only REST API.
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")  # nosec B104
    parser.add_argument("--port", type=int, default=8090, help="Join-token/enrollment/admin port")
    parser.add_argument(
        "--device-port",
        type=int,
        default=8091,
        help="mTLS port for heartbeat/certificate-rotation calls from already-enrolled devices",
    )
    parser.add_argument(
        "--cert-validity-days",
        type=int,
        default=90,
        help="Validity period for device and manager leaf certificates issued by the fleet CA",
    )
    parser.add_argument(
        "--ca-validity-days",
        type=int,
        default=3650,
        help="Validity period for the fleet CA's own self-signed certificate",
    )
    parser.add_argument(
        "--identity-hostname",
        default=_MANAGER_IDENTITY_COMMON_NAME,
        help="DNS name devices use to reach this manager, added as a Subject Alternative Name "
        "on its self-issued identity certificate alongside 127.0.0.1 (always included for "
        "loopback/local-dev reachability) — required for TLS hostname verification to pass "
        "against anything other than that DNS name",
    )
    parser.add_argument(
        "--schema-path",
        type=Path,
        default=None,
        help="Core config schema for validating a pushed config before queueing it "
        "(XEDGE-446). Default: auto-detected packaged/repo-root location.",
    )
    parser.add_argument("--version", action="version", version=f"xedge-fleet-manager {__version__}")
    return parser.parse_args(argv)


async def _serve(
    registry: DeviceRegistry,
    ca: CertificateAuthority,
    user_store: FleetUserStore,
    session_manager: FleetSessionManager,
    audit_log: FleetAuditLog,
    login_lockout: LoginLockout,
    *,
    cert_validity_days: int,
    config_validator: ConfigValidator,
    host: str,
    port: int,
    device_port: int,
    cert_path: Path,
    key_path: Path,
    ca_cert_path: Path,
    bootstrap_password_path: Path,
) -> None:
    """Builds both apps and bootstraps the default tenant/admin account
    *here*, inside the one `asyncio.run()` call that also serves them —
    not in `run()` beforehand. `DeviceRegistry`'s asyncpg connection pool
    is bound to whichever event loop first uses it; an earlier, separate
    `asyncio.run(registry.ensure_default_tenant())` call in `run()`
    created and tore down its own loop, and the pooled connection it left
    behind then failed with "attached to a different loop" the first time
    a real request here tried to reuse it (found by actually running the
    docker-compose stack end-to-end, not by the test suite -- pytest-
    asyncio keeps one loop per test, which this bug needs *two* separate
    `asyncio.run()` calls to reproduce)."""
    default_tenant_id = await registry.ensure_default_tenant()
    if not await user_store.has_any_users(default_tenant_id):
        # First-ever startup for this tenant (or every account was since
        # deleted -- either way, "zero accounts" gets a fresh bootstrap
        # rather than a permanent lockout).
        password = secrets.token_urlsafe(32)
        await user_store.create_user(default_tenant_id, "admin", password, "admin")
        # Blocking writes, but only ever once per manager lifetime, and
        # before either uvicorn server below has started accepting
        # connections -- there is no concurrent request this could ever
        # delay (same reasoning xedge.core.hot_reload's poll-loop stat()
        # already applies to a *much* more frequent blocking call).
        bootstrap_password_path.parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        bootstrap_password_path.write_text(password, encoding="utf-8")  # noqa: ASYNC240

    public_app = create_fleet_manager_app(
        registry,
        ca,
        user_store,
        session_manager,
        audit_log,
        login_lockout,
        cert_validity_days=cert_validity_days,
        config_validator=config_validator,
    )
    device_app = create_fleet_device_app(registry, ca, cert_validity_days=cert_validity_days)

    public_server = uvicorn.Server(
        uvicorn.Config(
            public_app,
            host=host,
            port=port,
            log_config=None,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
        )
    )
    device_server = uvicorn.Server(
        uvicorn.Config(
            device_app,
            host=host,
            port=device_port,
            log_config=None,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            ssl_ca_certs=str(ca_cert_path),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
        )
    )
    await asyncio.gather(public_server.serve(), device_server.serve())


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)

    run_migrations(args.database_url)
    engine = create_engine(args.database_url)
    registry = DeviceRegistry(engine)
    user_store = FleetUserStore(engine)
    session_manager = FleetSessionManager(engine)
    audit_log = FleetAuditLog(engine)
    login_lockout = LoginLockout()

    ca_cert_path = args.data_dir / "ca.pem"
    ca = load_or_create_ca(
        ca_cert_path, args.data_dir / "ca.key", _CA_COMMON_NAME, args.ca_validity_days
    )
    manager_cert_path = args.data_dir / "manager-cert.pem"
    manager_key_path = args.data_dir / "manager-key.pem"
    ca.issue_or_load_leaf_identity(
        manager_cert_path,
        manager_key_path,
        _MANAGER_IDENTITY_COMMON_NAME,
        args.cert_validity_days,
        san_dns_names=(args.identity_hostname,),
        san_ip_addresses=(ipaddress.ip_address("127.0.0.1"),),
    )

    schema_path = args.schema_path or _default_schema_path()
    config_validator = ConfigValidator.from_file(schema_path)

    asyncio.run(
        _serve(
            registry,
            ca,
            user_store,
            session_manager,
            audit_log,
            login_lockout,
            cert_validity_days=args.cert_validity_days,
            config_validator=config_validator,
            host=args.host,
            port=args.port,
            device_port=args.device_port,
            cert_path=manager_cert_path,
            key_path=manager_key_path,
            ca_cert_path=ca_cert_path,
            bootstrap_password_path=args.data_dir / "admin_bootstrap_password",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
