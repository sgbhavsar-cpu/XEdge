"""REST API v1 (FR-CM-003 / FR-OB-* / FR-WU-*): status, monitoring, and
full read-write configuration, backing both direct API consumers and the
local Web UI (xedge.api.ui, ADR-007).

Endpoints: `/health` (liveness, always unauthenticated), `/api/v1/status`,
`/api/v1/drivers`, `GET /api/v1/config` (secrets-safe, from version
history), `PUT /api/v1/config` (validate + write — the existing hot-reload
watcher applies it, so every property already established for hot-reload
applies automatically: version history, rollback, restart-only-affected-
drivers), and `/api/v1/auth/*` (single-user auth, ADR-007).

Auth is deliberately interim (FR-WU-008, HLR §4.9): one local account, no
RBAC yet. `xedge.core.main` binds this to loopback (127.0.0.1) by default —
now more load-bearing than before, since this API is write-capable.

`GET /api/v1/config` returns the latest saved config *version*
(ConfigVersionHistory), not `store.data` directly — the version history is
captured before secrets substitution, so this is the one place in the
codebase already guaranteed never to contain a resolved plaintext secret.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from xedge import __version__
from xedge.api.auth import SESSION_COOKIE_NAME as _SESSION_COOKIE_NAME
from xedge.api.auth import LoginAttemptTracker, SessionManager, UserStore
from xedge.api.config_ui import create_config_ui_router
from xedge.api.ui import create_ui_router
from xedge.core.config import ConfigValidationError, ConfigValidator, ConfigVersionHistory
from xedge.core.supervisor import DriverSupervisor
from xedge.northbound.dispatcher import NorthboundDispatcher

_STATIC_DIR = Path(__file__).parent / "static"


class _PasswordBody(BaseModel):
    password: str


def create_app(
    supervisor: DriverSupervisor,
    version_history: ConfigVersionHistory,
    dispatcher: NorthboundDispatcher | None = None,
    *,
    user_store: UserStore,
    session_manager: SessionManager,
    login_tracker: LoginAttemptTracker,
    config_path: Path,
    schema_path: Path,
) -> FastAPI:
    app = FastAPI(title="xEdge API", version=__version__)
    started_at = datetime.now(UTC)
    validator = ConfigValidator.from_file(schema_path)

    def require_auth(request: Request, response: Response) -> None:
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        refreshed = session_manager.refresh_if_valid(token)
        if refreshed is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
        # Sliding idle timeout: every authenticated request extends the
        # session, matching FR-WU-007's "idle timeout" (not a fixed
        # absolute expiry from login).
        response.set_cookie(
            _SESSION_COOKIE_NAME,
            refreshed,
            httponly=True,
            samesite="strict",
            # secure=False: plain HTTP until Sprint 13 (mTLS) lands, per
            # ADR-007 / system-architecture.md §6.1's documented port note.
            secure=False,
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/auth/status")
    def auth_status(request: Request) -> dict[str, bool]:
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        return {
            "account_exists": user_store.exists(),
            "authenticated": session_manager.refresh_if_valid(token) is not None,
        }

    @app.post("/api/v1/auth/setup")
    def auth_setup(body: _PasswordBody, response: Response) -> dict[str, str]:
        if user_store.exists():
            raise HTTPException(
                status.HTTP_409_CONFLICT, "An account already exists; use login instead"
            )
        user_store.create(body.password)
        token = session_manager.issue()
        response.set_cookie(
            _SESSION_COOKIE_NAME, token, httponly=True, samesite="strict", secure=False
        )
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    def auth_login(body: _PasswordBody, response: Response) -> dict[str, str]:
        if login_tracker.is_locked_out():
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many failed login attempts; try again later",
            )
        if not user_store.exists() or not user_store.verify(body.password):
            login_tracker.record_failure()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid password")
        login_tracker.record_success()
        token = session_manager.issue()
        response.set_cookie(
            _SESSION_COOKIE_NAME, token, httponly=True, samesite="strict", secure=False
        )
        return {"status": "ok"}

    @app.post("/api/v1/auth/logout")
    def auth_logout(response: Response) -> dict[str, str]:
        # Deliberately not behind require_auth: clearing your own cookie
        # needs no server-side permission check, and gating it on
        # require_auth previously caused a real bug — require_auth's
        # sliding-timeout cookie *refresh* raced with this handler's
        # delete on the same response, and the refresh won.
        response.delete_cookie(_SESSION_COOKIE_NAME)
        return {"status": "ok"}

    @app.get("/api/v1/status")
    def get_status(_auth: None = Depends(require_auth)) -> dict[str, Any]:
        return {
            "version": __version__,
            "started_at": started_at.isoformat(),
            "uptime_seconds": (datetime.now(UTC) - started_at).total_seconds(),
            "driver_count": len(supervisor.all_status()),
            "northbound_connected": dispatcher.connected if dispatcher is not None else None,
        }

    @app.get("/api/v1/drivers")
    def get_drivers(_auth: None = Depends(require_auth)) -> list[dict[str, Any]]:
        return [
            {
                "instance_id": status_.instance_id,
                "driver_type": status_.driver_type,
                "state": status_.state.value,
                "consecutive_failures": status_.consecutive_failures,
                "last_error": status_.last_error,
                "metrics": asdict(status_.metrics),
            }
            for status_ in supervisor.all_status().values()
        ]

    @app.get("/api/v1/config")
    def get_config(_auth: None = Depends(require_auth)) -> dict[str, Any]:
        versions = version_history.list_versions()
        if not versions:
            return {}
        return version_history.load_version(versions[-1])

    @app.put("/api/v1/config")
    def put_config(
        new_config: dict[str, Any], _auth: None = Depends(require_auth)
    ) -> dict[str, str]:
        """Validate, then write to the same file the hot-reload watcher
        already polls (FR-WU-005) — no separate UI-only mutation path.
        Secrets: the caller is responsible for sending `${SECRET:name}`
        placeholders back for any field it didn't intend to change (the UI
        never round-trips resolved secret values — see xedge.api.ui)."""
        try:
            validator.validate(new_config)
        except ConfigValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        config_path.write_text(yaml.safe_dump(new_config, sort_keys=False), encoding="utf-8")
        return {"status": "accepted"}

    app.mount("/ui-static", StaticFiles(directory=str(_STATIC_DIR)), name="ui-static")
    app.include_router(
        create_ui_router(
            supervisor,
            version_history,
            dispatcher,
            user_store=user_store,
            session_manager=session_manager,
            login_tracker=login_tracker,
        )
    )
    app.include_router(
        create_config_ui_router(
            session_manager=session_manager,
            config_path=config_path,
            core_schema_path=schema_path,
        )
    )

    return app
