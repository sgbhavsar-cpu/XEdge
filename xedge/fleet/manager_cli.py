"""`xedge-fleet-manager`: standalone entrypoint for the Fleet Manager
service (Sprint 29, XEDGE-211/215) — a separate process/container from any
individual xEdge device, per Sprint 32's documented split.

`--admin-token`/`--join-token` default to auto-generated, persisted-to-disk
secrets (same load-or-create pattern as `xedge.api.auth.load_or_create_secret_key`)
so a fresh deployment doesn't need either supplied up front, but reuses the
same value across restarts.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

import uvicorn

from xedge import __version__
from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.registry import DeviceRegistry


def _load_or_create_token(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="xedge-fleet-manager", description="xEdge Fleet Manager service"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("/data/fleet-manager"),
        help="Directory for the device registry database and auto-generated tokens",
    )
    # nosec B104 — a fleet manager must be reachable from every enrolled
    # device's network, unlike the per-device loopback-only REST API.
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")  # nosec B104
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--join-token",
        default=None,
        help="Shared secret devices present to enroll "
        "(default: auto-generated, persisted in --data-dir)",
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help="Bearer token for operator/CLI calls "
        "(default: auto-generated, persisted in --data-dir)",
    )
    parser.add_argument("--version", action="version", version=f"xedge-fleet-manager {__version__}")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    join_token = args.join_token or _load_or_create_token(args.data_dir / "join_token")
    admin_token = args.admin_token or _load_or_create_token(args.data_dir / "admin_token")
    registry = DeviceRegistry(args.data_dir / "devices.db")
    app = create_fleet_manager_app(registry, join_token=join_token, admin_token=admin_token)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(run())
