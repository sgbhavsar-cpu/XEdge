"""Local Web UI routes (ADR-007, HLR §4.9): server-rendered Jinja2 pages
sharing the same auth/config machinery as xedge.api.server's JSON API
(same UserStore/SessionManager/LoginAttemptTracker instances, same session
cookie — a browser logged in via these pages is also authenticated for the
JSON endpoints the dashboard's JS polls).

Kept as a separate router so the JSON API stays a clean, directly-
scriptable REST surface; this module is purely presentation. Unauthenticated
page requests redirect to /ui/login (a 401 JSON body would be wrong for a
browser navigation), unlike xedge.api.server's require_auth dependency.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from xedge.api.auth import SESSION_COOKIE_NAME, LoginAttemptTracker, SessionManager, UserStore
from xedge.core.config import ConfigValidationError, ConfigValidator, ConfigVersionHistory
from xedge.core.supervisor import DriverSupervisor
from xedge.northbound.dispatcher import NorthboundDispatcher

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_MIN_PASSWORD_LENGTH = 8


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        # Plain HTTP until Sprint 13 (mTLS) — see xedge.api.server's
        # matching note.
        secure=False,
    )


def create_ui_router(
    supervisor: DriverSupervisor,
    version_history: ConfigVersionHistory,
    dispatcher: NorthboundDispatcher | None,
    *,
    user_store: UserStore,
    session_manager: SessionManager,
    login_tracker: LoginAttemptTracker,
    config_path: Path,
    schema_path: Path,
) -> APIRouter:
    router = APIRouter(prefix="/ui")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    validator = ConfigValidator.from_file(schema_path)

    def is_authenticated(request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        return session_manager.refresh_if_valid(token) is not None

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
        token = session_manager.issue()
        response = RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(response, token)
        return response

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        if not user_store.exists():
            return RedirectResponse("/ui/setup", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @router.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, password: str = Form(...)) -> Response:
        if login_tracker.is_locked_out():
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Too many failed attempts; try again later"},
            )
        if not user_store.verify(password):
            login_tracker.record_failure()
            return templates.TemplateResponse(request, "login.html", {"error": "Invalid password"})
        login_tracker.record_success()
        token = session_manager.issue()
        response = RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(response, token)
        return response

    @router.post("/logout")
    def logout_submit() -> Response:
        response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        if not is_authenticated(request):
            return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
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
            },
        )

    @router.get("/config", response_class=HTMLResponse)
    def config_editor(request: Request) -> Response:
        if not is_authenticated(request):
            return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        versions = version_history.list_versions()
        current = version_history.load_version(versions[-1]) if versions else {}
        yaml_text = yaml.safe_dump(current, sort_keys=False)
        return templates.TemplateResponse(
            request, "config.html", {"yaml_text": yaml_text, "error": None, "success": False}
        )

    @router.post("/config", response_class=HTMLResponse)
    def config_submit(request: Request, yaml_text: str = Form(...)) -> Response:
        if not is_authenticated(request):
            return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        try:
            new_config = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            return templates.TemplateResponse(
                request,
                "config.html",
                {"yaml_text": yaml_text, "error": f"Invalid YAML: {exc}", "success": False},
            )
        if not isinstance(new_config, dict):
            return templates.TemplateResponse(
                request,
                "config.html",
                {
                    "yaml_text": yaml_text,
                    "error": "Config must be a YAML mapping (key: value), not a list/scalar",
                    "success": False,
                },
            )
        try:
            validator.validate(new_config)
        except ConfigValidationError as exc:
            return templates.TemplateResponse(
                request,
                "config.html",
                {"yaml_text": yaml_text, "error": str(exc), "success": False},
            )
        config_path.write_text(yaml_text, encoding="utf-8")
        return templates.TemplateResponse(
            request, "config.html", {"yaml_text": yaml_text, "error": None, "success": True}
        )

    return router
