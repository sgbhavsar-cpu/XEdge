"""REST API v1 (FR-CM-003 / FR-OB-*): a read-only status surface.

Endpoints: `/health` (liveness), `/api/v1/status` (app-level summary),
`/api/v1/drivers` (per-instance state + live metrics), `/api/v1/config`
(the config that's actually running, secrets never included).

No mutation endpoints and no authentication in this MVP — see
`docs/architecture/system-architecture.md` for the planned auth story
(operator RBAC over mTLS/HTTPS). Because of that, `xedge.core.main` binds
this to loopback (127.0.0.1) by default; the `api.host` config setting must
be changed explicitly (to a specific interface address, not 0.0.0.0) to
reach it from elsewhere on the OT network.

`/api/v1/config` returns the latest saved config *version* (ConfigVersionHistory),
not `store.data` directly — the version history is captured before secrets
substitution (see ConfigVersionHistory's docstring), so this is the one
place in the codebase already guaranteed never to contain a resolved
plaintext secret. Redacting `store.data` by key-name pattern instead would
risk missing an unexpected secret-bearing field name.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from xedge import __version__
from xedge.core.config import ConfigVersionHistory
from xedge.core.supervisor import DriverSupervisor
from xedge.northbound.dispatcher import NorthboundDispatcher


def create_app(
    supervisor: DriverSupervisor,
    version_history: ConfigVersionHistory,
    dispatcher: NorthboundDispatcher | None = None,
) -> FastAPI:
    app = FastAPI(title="xEdge API", version=__version__)
    started_at = datetime.now(UTC)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/status")
    def get_status() -> dict[str, Any]:
        return {
            "version": __version__,
            "started_at": started_at.isoformat(),
            "uptime_seconds": (datetime.now(UTC) - started_at).total_seconds(),
            "driver_count": len(supervisor.all_status()),
            "northbound_connected": dispatcher.connected if dispatcher is not None else None,
        }

    @app.get("/api/v1/drivers")
    def get_drivers() -> list[dict[str, Any]]:
        return [
            {
                "instance_id": status.instance_id,
                "driver_type": status.driver_type,
                "state": status.state.value,
                "consecutive_failures": status.consecutive_failures,
                "last_error": status.last_error,
                "metrics": asdict(status.metrics),
            }
            for status in supervisor.all_status().values()
        ]

    @app.get("/api/v1/config")
    def get_config() -> dict[str, Any]:
        versions = version_history.list_versions()
        if not versions:
            return {}
        return version_history.load_version(versions[-1])

    return app
