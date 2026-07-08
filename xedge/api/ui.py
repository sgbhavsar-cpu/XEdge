"""Local Web UI routes (ADR-007, HLR §4.9): server-rendered Jinja2 pages
sharing the same auth/config machinery as xedge.api.server's JSON API
(same UserStore/SessionManager/LoginAttemptTracker instances, same session
cookie — a browser logged in via these pages is also authenticated for the
JSON endpoints the dashboard's JS polls).

Kept as a separate router so the JSON API stays a clean, directly-
scriptable REST surface; this module is purely presentation. Unauthenticated
page requests redirect to /ui/login (a 401 JSON body would be wrong for a
browser navigation), unlike xedge.api.server's require_permission dependency.

RBAC (Sprint 14): `require_permission_redirect` is the page-view analog of
server.py's `require_permission` dependency — used here and re-exported for
xedge.api.config_ui to use too, so both routers share one permission-check
implementation instead of each re-deriving it (the pre-Sprint-14 code had
three independent copies of this logic; see xedge.api.auth.resolve_session).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from xedge.api.auth import (
    SESSION_COOKIE_NAME,
    AuthError,
    LoginAttemptTracker,
    SessionManager,
    UserStore,
    resolve_session,
)
from xedge.api.permissions import ROLE_PERMISSIONS, has_permission
from xedge.core.alarms import AlarmEngine
from xedge.core.config import ConfigVersionHistory
from xedge.core.supervisor import DriverSupervisor
from xedge.fleet.agent import FleetAgentStatus
from xedge.northbound.dispatcher import NorthboundDispatcher
from xedge.observability.audit_log import AuditLog

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_MIN_PASSWORD_LENGTH = 8


def _set_session_cookie(response: Response, token: str, *, secure_cookies: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        # Driven by the `tls` config section (Sprint 13, XEDGE-107/280) via
        # xedge.core.main — see xedge.api.server's matching note.
        secure=secure_cookies,
    )


def require_permission_redirect(
    request: Request, session_manager: SessionManager, user_store: UserStore, permission: str
) -> Response | None:
    """Page-view permission check: `None` if the session is valid and
    authorized for `permission`, else a redirect (to `/ui/login` if simply
    unauthenticated, `/ui/dashboard` if authenticated but lacking the
    permission — a 403 JSON body would be the wrong shape for a browser
    navigation, matching this module's existing "redirect, don't 401"
    posture)."""
    session = resolve_session(request, session_manager)
    if session is None:
        return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    _new_token, username = session
    role = user_store.get_role(username)
    if not has_permission(role, permission):
        return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return None


def create_ui_router(
    supervisor: DriverSupervisor,
    version_history: ConfigVersionHistory,  # noqa: ARG001 — kept for interface symmetry with config_ui's router factory
    dispatcher: NorthboundDispatcher | None,
    *,
    user_store: UserStore,
    session_manager: SessionManager,
    login_tracker: LoginAttemptTracker,
    audit_log: AuditLog,
    secure_cookies: bool = False,
    dashboard_url: str | None = None,
    fleet_status: FleetAgentStatus | None = None,
    alarm_engine: AlarmEngine | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/ui")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    def is_authenticated(request: Request) -> bool:
        return resolve_session(request, session_manager) is not None

    def nav_permissions(request: Request) -> dict[str, bool | str | None]:
        """Which optional nav links this session's role should see, plus the
        (permission-independent) tracing dashboard footer link — computed
        once per request rather than duplicating the permission matrix
        inside the templates themselves."""
        session = resolve_session(request, session_manager)
        role = user_store.get_role(session[1]) if session else None
        return {
            "can_view_config": has_permission(role, "config:read"),
            "can_manage_users": has_permission(role, "user:manage"),
            "can_view_audit_log": has_permission(role, "audit:read"),
            "can_restart_driver": has_permission(role, "driver:restart"),
            "can_manage_alarms": has_permission(role, "alarm:manage"),
            "dashboard_url": dashboard_url,
        }

    @router.get("", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        if not user_store.exists():
            return RedirectResponse("/ui/setup", status_code=status.HTTP_303_SEE_OTHER)
        if not is_authenticated(request):
            return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request) -> Response:
        if user_store.exists():
            return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request, "setup.html", {"error": None})

    @router.post("/setup", response_class=HTMLResponse)
    def setup_submit(
        request: Request, password: str = Form(...), confirm_password: str = Form(...)
    ) -> Response:
        if user_store.exists():
            return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        if password != confirm_password:
            return templates.TemplateResponse(
                request, "setup.html", {"error": "Passwords do not match"}
            )
        if len(password) < _MIN_PASSWORD_LENGTH:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {"error": f"Password must be at least {_MIN_PASSWORD_LENGTH} characters"},
            )
        user_store.create(password)
        token = session_manager.issue("admin")
        response = RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(response, token, secure_cookies=secure_cookies)
        audit_log.append("admin", "auth.setup")
        return response

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        if not user_store.exists():
            return RedirectResponse("/ui/setup", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @router.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request, username: str = Form("admin"), password: str = Form(...)
    ) -> Response:
        client_ip = request.client.host if request.client else None
        if login_tracker.is_locked_out():
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Too many failed attempts; try again later"},
            )
        if not user_store.verify(username, password):
            login_tracker.record_failure()
            audit_log.append(username, "auth.login_failure", {"ip": client_ip})
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid username or password"}
            )
        login_tracker.record_success()
        token = session_manager.issue(username)
        response = RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(response, token, secure_cookies=secure_cookies)
        audit_log.append(username, "auth.login_success", {"ip": client_ip})
        return response

    @router.post("/logout")
    def logout_submit(request: Request) -> Response:
        session = resolve_session(request, session_manager)
        if session is not None:
            audit_log.append(session[1], "auth.logout")
        response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "tag:read")
        if redirect is not None:
            return redirect
        drivers = [
            {
                "instance_id": s.instance_id,
                "driver_type": s.driver_type,
                "state": s.state.value,
                "consecutive_failures": s.consecutive_failures,
                "last_error": s.last_error,
                "metrics": s.metrics,
            }
            for s in supervisor.all_status().values()
        ]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "drivers": drivers,
                "northbound_connected": (dispatcher.connected if dispatcher is not None else None),
                "fleet_enabled": fleet_status.enabled if fleet_status is not None else False,
                "authenticated": True,
                **nav_permissions(request),
            },
        )

    @router.get("/dashboard/drivers/{instance_id}", response_class=HTMLResponse)
    def driver_detail(request: Request, instance_id: str) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "tag:read")
        if redirect is not None:
            return redirect
        all_status = supervisor.all_status()
        if instance_id not in all_status:
            return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        s = all_status[instance_id]
        return templates.TemplateResponse(
            request,
            "driver_detail.html",
            {
                "instance_id": s.instance_id,
                "driver_type": s.driver_type,
                "state": s.state.value,
                "consecutive_failures": s.consecutive_failures,
                "last_error": s.last_error,
                "metrics": s.metrics,
                "authenticated": True,
                **nav_permissions(request),
            },
        )

    @router.get("/logs", response_class=HTMLResponse)
    def logs(request: Request) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "tag:read")
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "logs.html",
            {
                "drivers": list(supervisor.all_status().keys()),
                "authenticated": True,
                **nav_permissions(request),
            },
        )

    @router.get("/diagnostics", response_class=HTMLResponse)
    def diagnostics_page(request: Request) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "tag:read")
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "diagnostics.html",
            {
                "authenticated": True,
                **nav_permissions(request),
            },
        )

    @router.get("/users", response_class=HTMLResponse)
    def users_page(request: Request) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "user:manage")
        if redirect is not None:
            return redirect
        session = resolve_session(request, session_manager)
        current_username = session[1] if session else None
        return templates.TemplateResponse(
            request,
            "users.html",
            {
                "users": user_store.list_users(),
                "roles": sorted(ROLE_PERMISSIONS.keys()),
                "current_username": current_username,
                "error": None,
                "authenticated": True,
                **nav_permissions(request),
            },
        )

    @router.post("/users/new", response_class=HTMLResponse)
    def users_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form(...),
    ) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "user:manage")
        if redirect is not None:
            return redirect
        actor = resolve_session(request, session_manager)[1]  # type: ignore[index]
        try:
            user_store.create_user(username, password, role)
        except AuthError as exc:
            return templates.TemplateResponse(
                request,
                "users.html",
                {
                    "users": user_store.list_users(),
                    "roles": sorted(ROLE_PERMISSIONS.keys()),
                    "current_username": actor,
                    "error": str(exc),
                    "authenticated": True,
                    **nav_permissions(request),
                },
            )
        audit_log.append(actor, "user.created", {"username": username, "role": role})
        return RedirectResponse("/ui/users", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/users/{username}/role")
    def users_set_role(request: Request, username: str, role: str = Form(...)) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "user:manage")
        if redirect is not None:
            return redirect
        actor = resolve_session(request, session_manager)[1]  # type: ignore[index]
        try:
            user_store.set_role(username, role)
        except AuthError:
            pass
        else:
            audit_log.append(actor, "user.role_changed", {"username": username, "role": role})
        return RedirectResponse("/ui/users", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/users/{username}/delete")
    def users_delete(request: Request, username: str) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "user:manage")
        if redirect is not None:
            return redirect
        session = resolve_session(request, session_manager)
        if session is not None and session[1] == username:
            return RedirectResponse("/ui/users", status_code=status.HTTP_303_SEE_OTHER)
        try:
            user_store.delete_user(username)
        except AuthError:
            pass
        else:
            if session is not None:
                audit_log.append(session[1], "user.deleted", {"username": username})
        return RedirectResponse("/ui/users", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "audit:read")
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "audit.html",
            {
                "entries": list(reversed(audit_log.tail(limit=500))),
                "chain_verified": audit_log.verify_chain(),
                "authenticated": True,
                **nav_permissions(request),
            },
        )

    @router.get("/alarms", response_class=HTMLResponse)
    def alarms_page(request: Request) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "tag:read")
        if redirect is not None:
            return redirect
        statuses = list(alarm_engine.all_status().values()) if alarm_engine is not None else []
        return templates.TemplateResponse(
            request,
            "alarms.html",
            {
                "alarm_engine_enabled": alarm_engine is not None,
                "statuses": sorted(statuses, key=lambda s: s.tag_id),
                "authenticated": True,
                **nav_permissions(request),
            },
        )

    @router.post("/alarms/{tag_id:path}/acknowledge")
    def alarms_acknowledge(request: Request, tag_id: str) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "alarm:manage")
        if redirect is not None:
            return redirect
        actor = resolve_session(request, session_manager)[1]  # type: ignore[index]
        if alarm_engine is not None and alarm_engine.acknowledge(tag_id, actor):
            audit_log.append(actor, "alarm.acknowledged", {"tag_id": tag_id})
        return RedirectResponse("/ui/alarms", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/alarms/{tag_id:path}/shelve")
    def alarms_shelve(
        request: Request, tag_id: str, duration_seconds: float = Form(...)
    ) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "alarm:manage")
        if redirect is not None:
            return redirect
        if alarm_engine is not None:
            alarm_engine.shelve(tag_id, duration_seconds)
            actor = resolve_session(request, session_manager)[1]  # type: ignore[index]
            audit_log.append(
                actor, "alarm.shelved", {"tag_id": tag_id, "duration_seconds": duration_seconds}
            )
        return RedirectResponse("/ui/alarms", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/alarms/{tag_id:path}/unshelve")
    def alarms_unshelve(request: Request, tag_id: str) -> Response:
        redirect = require_permission_redirect(request, session_manager, user_store, "alarm:manage")
        if redirect is not None:
            return redirect
        if alarm_engine is not None and alarm_engine.unshelve(tag_id):
            actor = resolve_session(request, session_manager)[1]  # type: ignore[index]
            audit_log.append(actor, "alarm.unshelved", {"tag_id": tag_id})
        return RedirectResponse("/ui/alarms", status_code=status.HTTP_303_SEE_OTHER)

    # Note: /ui/config* is handled by xedge.api.config_ui's dedicated
    # router (schema-driven tree + forms) — not here. This module only
    # renders login/setup/dashboard/driver-detail/logs/users/audit/alarms/
    # logout.

    return router
