"""Schema-driven config tree + forms (ADR-007 follow-up, FR-WU-003/010):
replaces the raw-YAML textarea as the primary way to edit configuration
through the browser. A left-side tree (Core Settings, then Drivers ->
each instance -> Tag Groups -> each group -> Tags) navigates to a form per
node, built from xedge.api.schema_forms + the JSON Schema files already
used to validate xedge.yaml — so a new driver type or field automatically
gets a working, correctly-typed form with no new template code.

Each tree node is edited as its own flat form (see schema_forms' module
docstring for why); child collections are shown as a small table with
edit/delete links plus an "add" link, not inlined into the parent's form.
Every write re-validates against the core schema, and for driver-owned
nodes (a driver / its tag groups / its tags) *also* against that driver
type's own schema (config/schema/drivers/<type>.schema.json) — the core
schema alone only checks a driver entry's `id`/`type`/`enabled`, never its
`config`/`tag_groups` shape, so without this a bad Modbus address or a
missing OPC UA endpoint_url would only surface later when the driver fails
to start, not at save time.

The raw-YAML editor from the previous round is kept as an "Advanced" escape
hatch (some things — like reordering tag groups, or config this UI doesn't
have a screen for yet — are still easiest to hand-edit), reachable from the
tree, not the default view.

RBAC (Sprint 14): every route requires `config:read` (view) or
`config:write` (any mutating POST) via `xedge.api.ui`'s shared
`require_permission_redirect` — this module previously hand-rolled its own
`is_authenticated`/`require_auth_redirect`, now replaced with the same
helper `xedge.api.server`'s JSON API and `xedge.api.ui`'s pages use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from xedge.api.auth import SessionManager, UserStore, resolve_session
from xedge.api.permissions import has_permission
from xedge.api.schema_forms import build_object_fields, unflatten
from xedge.api.tag_bulk_io import TagBulkParseError, tags_from_csv, tags_from_json, tags_to_csv
from xedge.api.ui import require_permission_redirect
from xedge.core.assets import all_tag_refs
from xedge.core.config import ConfigValidationError, ConfigValidator
from xedge.core.driver_config import driver_type_schema_path
from xedge.observability.audit_log import AuditLog

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Kept in sync by hand with xedge.core.main._build_registry()'s registered
# types — a UI-only list of type *strings* so this module doesn't need to
# import driver implementation classes just to populate a dropdown.
KNOWN_DRIVER_TYPES = [
    "modbus_tcp",
    "modbus_rtu_tcp",
    "modbus_rtu_serial",
    "opcua_client",
    "bacnet_ip",
    "ethernet_ip",
    "mqtt_subscriber",
    "snmp_client",
    "snmp_trap_receiver",
    "loopback",
]

# "id" is treated as immutable once a tag group/tag exists — these routes
# key lookups and URLs off it, and changing it here would desync both
# without a redirect this module doesn't implement. Excluded from both the
# rendered form (build_object_fields) and the parsed submission
# (unflatten) so it can never be edited away by this UI.
_SKIP_ID = frozenset({"id"})
_SKIP_ID_AND_TAGS = frozenset({"id", "tags"})
# Asset parameters (ADR-010's tag_ref list) are a child collection with
# their own add/delete routes below, same reasoning as tags on a driver's
# tag group -- not inlined into the asset's own metadata form.
_SKIP_ID_AND_PARAMETERS = frozenset({"id", "parameters"})

# mqtt_broker's users/publish_acl/subscribe_acl are all keyed collections
# (a list of credentials; two dicts keyed by arbitrary usernames) that
# xedge.api.schema_forms has no widget for (it renders scalars and fixed-
# property objects only) -- the same pre-existing gap alarms.rules (an
# array) already has, covered there by the raw-YAML "Advanced" editor.
# `users` gets its own small dedicated page instead (see the
# mqtt-broker/users routes below) because, unlike alarms.rules, the
# generic form's plain-text fallback would render this one's *plaintext
# passwords* directly into the page — not just an unusable widget, a
# credential-exposure rough edge. publish_acl/subscribe_acl stay on the
# Advanced editor for now; XEDGE-455 is scoped to config *usability*, not
# to teaching schema_forms a general keyed-collection widget.
_SKIP_MQTT_BROKER_MANAGED_FIELDS = frozenset({"users", "publish_acl", "subscribe_acl"})

# smtp.alarm_notifications is a nested object whose own `recipients` is a
# plain string array; smtp.scheduled_reports is an array of objects. Same
# schema_forms gap as mqtt_broker's fields above (no keyed-collection/
# array widget) -- neither involves secrets the way mqtt_broker.users
# did, so both simply stay on the Advanced editor rather than getting a
# dedicated page, with a note on the SMTP settings page pointing there.
_SKIP_SMTP_MANAGED_FIELDS = frozenset({"alarm_notifications", "scheduled_reports"})

# snmp_notify.destinations is an array of objects, the same schema_forms
# gap as smtp.scheduled_reports above -- stays on the Advanced editor for
# the same reason (XEDGE-486 was scoped to the SNMP client/agent config
# screens, not to teaching schema_forms a general array widget a fourth
# time).
_SKIP_SNMP_NOTIFY_MANAGED_FIELDS = frozenset({"destinations"})

CORE_SECTIONS = [
    ("logging", "Logging"),
    ("watchdog", "Watchdog"),
    ("northbound", "Northbound (MQTT)"),
    ("opcua_server", "OPC UA Server"),
    ("mqtt_broker", "MQTT Broker"),
    ("smtp", "SMTP"),
    ("snmp_agent", "SNMP Agent"),
    ("snmp_notify", "SNMP TRAP/INFORM"),
    ("store", "Store & Forward"),
    ("config_management", "Config Management"),
    ("api", "REST API / Web UI"),
    ("tls", "TLS"),
    ("tracing", "Tracing"),
    ("metrics", "Metrics"),
    ("rate_limit", "Rate Limiting"),
    ("fleet", "Fleet Management"),
    ("alarms", "Alarms"),
]


class ConfigNotFoundError(Exception):
    """Raised when a tree-node lookup (driver/tag_group/tag id) misses —
    turned into a 404 by the route layer."""


def _full_config(config_path: Path) -> dict[str, Any]:
    """Read the config directly from the file the forms write to — not
    from ConfigVersionHistory, which is only populated asynchronously by
    the hot-reload poll loop (xedge.core.hot_reload) and would otherwise
    show stale data for a brief window (the poll interval, e.g. 2s) right
    after any write, including to a request that immediately follows a
    redirect to the entity it just created. The file never contains
    resolved secrets (only `${SECRET:name}` placeholders — resolution
    happens elsewhere, at driver-start time), so this is exactly as safe
    to render as version_history was."""
    if not config_path.is_file():
        return {"schema_version": "0.1"}
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {"schema_version": "0.1"}


def _find_driver(config: dict[str, Any], driver_id: str) -> dict[str, Any]:
    drivers: list[dict[str, Any]] = config.get("drivers", [])
    for entry in drivers:
        if entry.get("id") == driver_id:
            return entry
    raise ConfigNotFoundError(f"No driver with id {driver_id!r}")


def _find_tag_group(driver_entry: dict[str, Any], group_id: str) -> dict[str, Any]:
    groups: list[dict[str, Any]] = driver_entry.get("tag_groups", [])
    for group in groups:
        if group.get("id") == group_id:
            return group
    raise ConfigNotFoundError(f"No tag group with id {group_id!r}")


def _find_tag(group_entry: dict[str, Any], tag_id: str) -> dict[str, Any]:
    tags: list[dict[str, Any]] = group_entry.get("tags", [])
    for tag in tags:
        if tag.get("id") == tag_id:
            return tag
    raise ConfigNotFoundError(f"No tag with id {tag_id!r}")


def _find_asset(config: dict[str, Any], asset_id: str) -> dict[str, Any]:
    assets: list[dict[str, Any]] = config.get("assets", [])
    for entry in assets:
        if entry.get("id") == asset_id:
            return entry
    raise ConfigNotFoundError(f"No asset with id {asset_id!r}")


def create_config_ui_router(
    *,
    session_manager: SessionManager,
    user_store: UserStore,
    audit_log: AuditLog,
    config_path: Path,
    core_schema_path: Path,
    dashboard_url: str | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/ui/config")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    core_validator = ConfigValidator.from_file(core_schema_path)
    core_schema = json.loads(core_schema_path.read_text(encoding="utf-8"))
    _driver_schema_cache: dict[str, dict[str, Any]] = {}

    def driver_type_schema(driver_type: str) -> dict[str, Any]:
        cached = _driver_schema_cache.get(driver_type)
        if cached is None:
            schema_path = driver_type_schema_path(driver_type)
            cached = json.loads(schema_path.read_text(encoding="utf-8"))
            _driver_schema_cache[driver_type] = cached
        return cached

    def require_read(request: Request) -> Response | None:
        return require_permission_redirect(request, session_manager, user_store, "config:read")

    def require_write(request: Request) -> Response | None:
        return require_permission_redirect(request, session_manager, user_store, "config:write")

    def nav_permissions(request: Request) -> dict[str, bool | str | None]:
        session = resolve_session(request, session_manager)
        role = user_store.get_role(session[1]) if session else None
        return {
            "can_manage_users": has_permission(role, "user:manage"),
            "can_view_audit_log": has_permission(role, "audit:read"),
            "dashboard_url": dashboard_url,
        }

    def render(request: Request, template: str, context: dict[str, Any]) -> Response:
        current = _full_config(config_path)
        base_context = {
            "core_sections": CORE_SECTIONS,
            "drivers": current.get("drivers", []),
            "assets": current.get("assets", []),
            "error": context.pop("error", None),
            "success": context.pop("success", False),
            "authenticated": True,
            **nav_permissions(request),
        }
        base_context.update(context)
        return templates.TemplateResponse(request, template, base_context)

    def _save_full_config(new_config: dict[str, Any], request: Request) -> str | None:
        """Validate against the core schema and write, letting hot-reload
        apply it — same rule as the raw-YAML editor and the JSON API's PUT
        /api/v1/config (ADR-007: one write path, not a UI-only mutation
        mechanism). Returns an error message, or None on success.

        The single shared save path for every mutating route in this
        module, so it's also the single place that records a
        `config.write` audit event (Sprint 15, XEDGE-119) rather than
        threading an audit call through every caller."""
        try:
            core_validator.validate(new_config)
        except ConfigValidationError as exc:
            return str(exc)
        config_path.write_text(yaml.safe_dump(new_config, sort_keys=False), encoding="utf-8")
        session = resolve_session(request, session_manager)
        if session is not None:
            audit_log.append(session[1], "config.write")
        return None

    def _validate_driver_section(
        driver_type: str, config: dict[str, Any], tag_groups: list[Any]
    ) -> str | None:
        """The core schema doesn't check a driver's own config/tag_groups
        shape (only id/type/enabled) — validate against that driver type's
        own schema too, the same one xedge.core.driver_config uses at
        startup, so a bad value is caught at save time, not when the
        driver fails to start."""
        schema_path = driver_type_schema_path(driver_type)
        if not schema_path.is_file():
            return f"Unknown driver type {driver_type!r} (no schema found)"
        try:
            ConfigValidator(driver_type_schema(driver_type)).validate(
                {"config": config, "tag_groups": tag_groups}
            )
        except ConfigValidationError as exc:
            return str(exc)
        return None

    # ---- Landing page ----

    @router.get("", response_class=HTMLResponse)
    def config_root(request: Request) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        return render(request, "config_root.html", {})

    # ---- Core sections ----

    def _core_section_skip(section: str) -> frozenset[str]:
        if section == "mqtt_broker":
            return _SKIP_MQTT_BROKER_MANAGED_FIELDS
        if section == "smtp":
            return _SKIP_SMTP_MANAGED_FIELDS
        if section == "snmp_notify":
            return _SKIP_SNMP_NOTIFY_MANAGED_FIELDS
        return frozenset()

    @router.get("/core/{section}", response_class=HTMLResponse)
    def core_section_form(request: Request, section: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        section_names = {key for key, _ in CORE_SECTIONS}
        if section not in section_names:
            return render(request, "config_root.html", {"error": f"Unknown section {section!r}"})
        section_schema = core_schema["properties"][section]
        current = _full_config(config_path)
        fields = build_object_fields(
            section_schema, current.get(section, {}), skip=_core_section_skip(section)
        )
        label = dict(CORE_SECTIONS)[section]
        return render(
            request, "config_section.html", {"section": section, "label": label, "fields": fields}
        )

    @router.post("/core/{section}", response_class=HTMLResponse)
    async def core_section_save(request: Request, section: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        raw_form = await request.form()
        form_data: dict[str, str] = {k: v for k, v in raw_form.items() if isinstance(v, str)}
        section_schema = core_schema["properties"][section]
        current = _full_config(config_path)
        skip = _core_section_skip(section)
        new_section = unflatten(form_data, section_schema, skip=skip)
        # Preserve any secret currently set if the form's password field
        # was left blank (FR-WU-006's "leave blank to keep").
        _merge_preserving_absent_secrets(section_schema, current.get(section, {}), new_section)
        # In place, and only over the schema's own properties minus `skip`
        # -- mqtt_broker's users/publish_acl/subscribe_acl (see
        # _SKIP_MQTT_BROKER_MANAGED_FIELDS) never appeared in this form, so
        # a plain `current[section] = new_section` would silently delete
        # them on every unrelated save (e.g. just flipping tls_enabled).
        _replace_editable_fields(current.setdefault(section, {}), section_schema, new_section, skip)
        error = _save_full_config(current, request)
        label = dict(CORE_SECTIONS)[section]
        fields = build_object_fields(section_schema, new_section, skip=skip)
        return render(
            request,
            "config_section.html",
            {
                "section": section,
                "label": label,
                "fields": fields,
                "error": error,
                "success": error is None,
            },
        )

    # ---- MQTT broker users (XEDGE-454/455) ----
    #
    # A dedicated list/add/delete page rather than a generic-form field:
    # see _SKIP_MQTT_BROKER_MANAGED_FIELDS above for why. Reads/writes
    # mqtt_broker.users directly in the config file, same one write path
    # (_save_full_config) as every other mutation in this module.

    @router.get("/mqtt-broker/users", response_class=HTMLResponse)
    def mqtt_broker_users_page(request: Request) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        broker_config = _full_config(config_path).get("mqtt_broker", {})
        usernames = [u.get("username") for u in broker_config.get("users", [])]
        return render(
            request, "mqtt_broker_users.html", {"usernames": usernames, "add_error": None}
        )

    @router.post("/mqtt-broker/users/new", response_class=HTMLResponse)
    def mqtt_broker_users_create(
        request: Request, username: str = Form(...), password: str = Form(...)
    ) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        broker_config = current.setdefault("mqtt_broker", {})
        users = broker_config.setdefault("users", [])
        if any(u.get("username") == username for u in users):
            return render(
                request,
                "mqtt_broker_users.html",
                {
                    "usernames": [u.get("username") for u in users],
                    "add_error": f"A broker user named {username!r} already exists",
                },
            )
        users.append({"username": username, "password": password})
        error = _save_full_config(current, request)
        if error is not None:
            users.pop()
            return render(
                request,
                "mqtt_broker_users.html",
                {"usernames": [u.get("username") for u in users], "add_error": error},
            )
        return RedirectResponse(
            "/ui/config/mqtt-broker/users", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/mqtt-broker/users/{username}/delete")
    def mqtt_broker_users_delete(request: Request, username: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        broker_config = current.setdefault("mqtt_broker", {})
        broker_config["users"] = [
            u for u in broker_config.get("users", []) if u.get("username") != username
        ]
        _save_full_config(current, request)
        return RedirectResponse(
            "/ui/config/mqtt-broker/users", status_code=status.HTTP_303_SEE_OTHER
        )

    # ---- Drivers: list / add / edit / delete ----

    @router.get("/drivers/new", response_class=HTMLResponse)
    def driver_new_form(request: Request) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        return render(
            request,
            "driver_new.html",
            {"driver_types": KNOWN_DRIVER_TYPES, "error": None, "id_value": "", "type_value": ""},
        )

    @router.post("/drivers/new", response_class=HTMLResponse)
    async def driver_new_submit(
        request: Request, id: str = Form(...), type: str = Form(...)
    ) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        drivers = current.setdefault("drivers", [])
        if any(d.get("id") == id for d in drivers):
            return render(
                request,
                "driver_new.html",
                {
                    "driver_types": KNOWN_DRIVER_TYPES,
                    "error": f"A driver with id {id!r} already exists",
                    "id_value": id,
                    "type_value": type,
                },
            )
        if type not in KNOWN_DRIVER_TYPES:
            return render(
                request,
                "driver_new.html",
                {
                    "driver_types": KNOWN_DRIVER_TYPES,
                    "error": f"Unknown driver type {type!r}",
                    "id_value": id,
                    "type_value": type,
                },
            )
        drivers.append({"id": id, "type": type, "enabled": True, "config": {}, "tag_groups": []})
        error = _save_full_config(current, request)
        if error is not None:
            drivers.pop()
            return render(
                request,
                "driver_new.html",
                {
                    "driver_types": KNOWN_DRIVER_TYPES,
                    "error": error,
                    "id_value": id,
                    "type_value": type,
                },
            )
        return RedirectResponse(f"/ui/config/drivers/{id}", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/drivers/{driver_id}", response_class=HTMLResponse)
    def driver_form(request: Request, driver_id: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        type_schema = driver_type_schema(entry["type"])
        fields = build_object_fields(type_schema["properties"]["config"], entry.get("config", {}))
        return render(
            request,
            "driver_edit.html",
            {
                "driver_id": driver_id,
                "driver_type": entry["type"],
                "enabled": entry.get("enabled", True),
                "fields": fields,
                "tag_groups": entry.get("tag_groups", []),
            },
        )

    @router.post("/drivers/{driver_id}", response_class=HTMLResponse)
    async def driver_save(request: Request, driver_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})

        raw_form = await request.form()
        form_data: dict[str, str] = {k: v for k, v in raw_form.items() if isinstance(v, str)}
        type_schema = driver_type_schema(entry["type"])
        config_schema = type_schema["properties"]["config"]
        new_config = unflatten(form_data, config_schema)
        _merge_preserving_absent_secrets(config_schema, entry.get("config", {}), new_config)
        entry["enabled"] = "enabled" in form_data
        entry["config"] = new_config

        error = _validate_driver_section(entry["type"], new_config, entry.get("tag_groups", []))
        if error is None:
            error = _save_full_config(current, request)
        fields = build_object_fields(config_schema, new_config)
        return render(
            request,
            "driver_edit.html",
            {
                "driver_id": driver_id,
                "driver_type": entry["type"],
                "enabled": entry["enabled"],
                "fields": fields,
                "tag_groups": entry.get("tag_groups", []),
                "error": error,
                "success": error is None,
            },
        )

    @router.post("/drivers/{driver_id}/validate")
    async def driver_validate(request: Request, driver_id: str) -> Response:
        """Dry-run only (Sprint 25, XEDGE-187/292) — same unflatten +
        per-driver-type validation the Save & Deploy path uses, but never
        calls `_save_full_config`. Returns a small JSON body (not a
        redirect/full page) so the "Validate" button can show the result
        inline without navigating away, matching the story's "preview...
        in the config editor's save flow."""
        redirect = require_read(request)
        if redirect is not None:
            return JSONResponse({"valid": False, "error": "Not authorized"}, status_code=403)
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
        except ConfigNotFoundError as exc:
            return JSONResponse({"valid": False, "error": str(exc)}, status_code=404)

        raw_form = await request.form()
        form_data: dict[str, str] = {k: v for k, v in raw_form.items() if isinstance(v, str)}
        config_schema = driver_type_schema(entry["type"])["properties"]["config"]
        new_config = unflatten(form_data, config_schema)
        _merge_preserving_absent_secrets(config_schema, entry.get("config", {}), new_config)

        error = _validate_driver_section(entry["type"], new_config, entry.get("tag_groups", []))
        return JSONResponse({"valid": error is None, "error": error})

    @router.post("/drivers/{driver_id}/delete")
    def driver_delete(request: Request, driver_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        current["drivers"] = [d for d in current.get("drivers", []) if d.get("id") != driver_id]
        error = _save_full_config(current, request)
        if error is not None:
            # Deletion was rejected (XEDGE-461: an asset still references
            # one of this driver's tags) -- the file was left untouched,
            # so re-render this still-existing driver's own edit page
            # with the error instead of redirecting as if it had worked.
            config_schema = driver_type_schema(entry["type"])["properties"]["config"]
            fields = build_object_fields(config_schema, entry.get("config", {}))
            return render(
                request,
                "driver_edit.html",
                {
                    "driver_id": driver_id,
                    "driver_type": entry["type"],
                    "enabled": entry.get("enabled", True),
                    "fields": fields,
                    "tag_groups": entry.get("tag_groups", []),
                    "error": error,
                },
            )
        return RedirectResponse("/ui/config", status_code=status.HTTP_303_SEE_OTHER)

    # ---- Tag groups ----

    @router.get("/drivers/{driver_id}/tag-groups/new", response_class=HTMLResponse)
    def tag_group_new_form(request: Request, driver_id: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        return render(
            request, "tag_group_new.html", {"driver_id": driver_id, "error": None, "id_value": ""}
        )

    @router.post("/drivers/{driver_id}/tag-groups/new", response_class=HTMLResponse)
    def tag_group_new_submit(request: Request, driver_id: str, id: str = Form(...)) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        groups = entry.setdefault("tag_groups", [])
        if any(g.get("id") == id for g in groups):
            return render(
                request,
                "tag_group_new.html",
                {
                    "driver_id": driver_id,
                    "error": f"A tag group with id {id!r} already exists on this driver",
                    "id_value": id,
                },
            )
        groups.append({"id": id, "scan_rate_ms": 1000, "tags": []})
        # New group has an empty tags list, which fails "minItems: 1" per
        # driver-type schemas — that's expected and surfaced once the
        # operator tries to save the group without adding a tag; the group
        # itself is allowed to exist in-progress in the on-disk config
        # since the core schema doesn't check tag_groups shape at all.
        _save_full_config(current, request)
        return RedirectResponse(
            f"/ui/config/drivers/{driver_id}/tag-groups/{id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.get("/drivers/{driver_id}/tag-groups/{group_id}", response_class=HTMLResponse)
    def tag_group_form(request: Request, driver_id: str, group_id: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
            group = _find_tag_group(entry, group_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        type_schema = driver_type_schema(entry["type"])
        group_schema = type_schema["properties"]["tag_groups"]["items"]
        fields = build_object_fields(group_schema, group, skip=_SKIP_ID_AND_TAGS)
        return render(
            request,
            "tag_group_edit.html",
            {
                "driver_id": driver_id,
                "group_id": group_id,
                "fields": fields,
                "tags": group.get("tags", []),
            },
        )

    @router.post("/drivers/{driver_id}/tag-groups/{group_id}", response_class=HTMLResponse)
    async def tag_group_save(request: Request, driver_id: str, group_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
            group = _find_tag_group(entry, group_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})

        raw_form = await request.form()
        form_data: dict[str, str] = {k: v for k, v in raw_form.items() if isinstance(v, str)}
        type_schema = driver_type_schema(entry["type"])
        group_schema = type_schema["properties"]["tag_groups"]["items"]
        new_group_fields = unflatten(form_data, group_schema, skip=_SKIP_ID_AND_TAGS)
        _replace_editable_fields(group, group_schema, new_group_fields, skip=_SKIP_ID_AND_TAGS)

        error = _validate_driver_section(
            entry["type"], entry.get("config", {}), entry.get("tag_groups", [])
        )
        if error is None:
            error = _save_full_config(current, request)
        fields = build_object_fields(group_schema, group, skip=_SKIP_ID_AND_TAGS)
        return render(
            request,
            "tag_group_edit.html",
            {
                "driver_id": driver_id,
                "group_id": group.get("id", group_id),
                "fields": fields,
                "tags": group.get("tags", []),
                "error": error,
                "success": error is None,
            },
        )

    @router.get("/drivers/{driver_id}/tag-groups/{group_id}/tags/export.csv")
    def tags_export_csv(request: Request, driver_id: str, group_id: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
            group = _find_tag_group(entry, group_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        type_schema = driver_type_schema(entry["type"])
        tag_schema = type_schema["properties"]["tag_groups"]["items"]["properties"]["tags"]["items"]
        csv_text = tags_to_csv(group.get("tags", []), tag_schema)
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{driver_id}_{group_id}_tags.csv"'
            },
        )

    @router.post(
        "/drivers/{driver_id}/tag-groups/{group_id}/tags/import", response_class=HTMLResponse
    )
    async def tags_import_submit(
        request: Request, driver_id: str, group_id: str, file: UploadFile = File(...)
    ) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
            group = _find_tag_group(entry, group_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})

        type_schema = driver_type_schema(entry["type"])
        group_schema = type_schema["properties"]["tag_groups"]["items"]
        tag_schema = group_schema["properties"]["tags"]["items"]
        raw = await file.read()
        try:
            text = raw.decode("utf-8")
            is_json = (file.filename or "").lower().endswith(".json")
            new_tags = tags_from_json(text) if is_json else tags_from_csv(text, tag_schema)
        except (TagBulkParseError, UnicodeDecodeError) as exc:
            error: str | None = str(exc)
            new_tags = None

        if new_tags is not None:
            # Upsert by id: re-importing a previously-exported (and
            # edited) CSV updates existing tags rather than duplicating
            # them, while still adding genuinely new ids — the "mass
            # commissioning" use case needs both.
            by_id = {t.get("id"): t for t in group.get("tags", [])}
            for tag in new_tags:
                by_id[tag["id"]] = tag
            candidate_tags = list(by_id.values())
            candidate_tag_groups = [
                {**g, "tags": candidate_tags} if g is group else g
                for g in entry.get("tag_groups", [])
            ]
            error = _validate_driver_section(
                entry["type"], entry.get("config", {}), candidate_tag_groups
            )
            if error is None:
                group["tags"] = candidate_tags
                error = _save_full_config(current, request)

        fields = build_object_fields(group_schema, group, skip=_SKIP_ID_AND_TAGS)
        return render(
            request,
            "tag_group_edit.html",
            {
                "driver_id": driver_id,
                "group_id": group.get("id", group_id),
                "fields": fields,
                "tags": group.get("tags", []),
                "error": error,
                "success": error is None,
            },
        )

    @router.post("/drivers/{driver_id}/tag-groups/{group_id}/delete")
    def tag_group_delete(request: Request, driver_id: str, group_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        original_tag_groups = entry.get("tag_groups", [])
        entry["tag_groups"] = [g for g in original_tag_groups if g.get("id") != group_id]
        error = _save_full_config(current, request)
        if error is not None:
            # XEDGE-461: an asset still references one of this group's
            # tags -- nothing was written to disk, so undo the in-memory
            # removal too (entry is mutated in place above) before
            # re-rendering the driver's own page with the error, rather
            # than showing the group as already gone.
            entry["tag_groups"] = original_tag_groups
            config_schema = driver_type_schema(entry["type"])["properties"]["config"]
            fields = build_object_fields(config_schema, entry.get("config", {}))
            return render(
                request,
                "driver_edit.html",
                {
                    "driver_id": driver_id,
                    "driver_type": entry["type"],
                    "enabled": entry.get("enabled", True),
                    "fields": fields,
                    "tag_groups": entry.get("tag_groups", []),
                    "error": error,
                },
            )
        return RedirectResponse(
            f"/ui/config/drivers/{driver_id}", status_code=status.HTTP_303_SEE_OTHER
        )

    # ---- Tags ----

    @router.get("/drivers/{driver_id}/tag-groups/{group_id}/tags/new", response_class=HTMLResponse)
    def tag_new_form(request: Request, driver_id: str, group_id: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        return render(
            request,
            "tag_new.html",
            {"driver_id": driver_id, "group_id": group_id, "error": None, "id_value": ""},
        )

    @router.post("/drivers/{driver_id}/tag-groups/{group_id}/tags/new", response_class=HTMLResponse)
    def tag_new_submit(
        request: Request, driver_id: str, group_id: str, id: str = Form(...)
    ) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
            group = _find_tag_group(entry, group_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        tags = group.setdefault("tags", [])
        if any(t.get("id") == id for t in tags):
            return render(
                request,
                "tag_new.html",
                {
                    "driver_id": driver_id,
                    "group_id": group_id,
                    "error": f"A tag with id {id!r} already exists in this group",
                    "id_value": id,
                },
            )
        # A minimal, likely-invalid stub — the operator fills in the real
        # fields (function_code/address or node_id, depending on driver
        # type) on the edit form that follows; validation runs on that save.
        tags.append({"id": id})
        _save_full_config(current, request)
        return RedirectResponse(
            f"/ui/config/drivers/{driver_id}/tag-groups/{group_id}/tags/{id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.get(
        "/drivers/{driver_id}/tag-groups/{group_id}/tags/{tag_id}", response_class=HTMLResponse
    )
    def tag_form(request: Request, driver_id: str, group_id: str, tag_id: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
            group = _find_tag_group(entry, group_id)
            tag = _find_tag(group, tag_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        type_schema = driver_type_schema(entry["type"])
        tag_schema = type_schema["properties"]["tag_groups"]["items"]["properties"]["tags"]["items"]
        fields = build_object_fields(tag_schema, tag, skip=_SKIP_ID)
        return render(
            request,
            "tag_edit.html",
            {"driver_id": driver_id, "group_id": group_id, "tag_id": tag_id, "fields": fields},
        )

    @router.post(
        "/drivers/{driver_id}/tag-groups/{group_id}/tags/{tag_id}", response_class=HTMLResponse
    )
    async def tag_save(request: Request, driver_id: str, group_id: str, tag_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
            group = _find_tag_group(entry, group_id)
            tag = _find_tag(group, tag_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})

        raw_form = await request.form()
        form_data: dict[str, str] = {k: v for k, v in raw_form.items() if isinstance(v, str)}
        type_schema = driver_type_schema(entry["type"])
        tag_schema = type_schema["properties"]["tag_groups"]["items"]["properties"]["tags"]["items"]
        new_tag_fields = unflatten(form_data, tag_schema, skip=_SKIP_ID)
        _replace_editable_fields(tag, tag_schema, new_tag_fields, skip=_SKIP_ID)

        error = _validate_driver_section(
            entry["type"], entry.get("config", {}), entry.get("tag_groups", [])
        )
        if error is None:
            error = _save_full_config(current, request)
        fields = build_object_fields(tag_schema, tag, skip=_SKIP_ID)
        return render(
            request,
            "tag_edit.html",
            {
                "driver_id": driver_id,
                "group_id": group_id,
                "tag_id": tag.get("id", tag_id),
                "fields": fields,
                "error": error,
                "success": error is None,
            },
        )

    @router.post("/drivers/{driver_id}/tag-groups/{group_id}/tags/{tag_id}/delete")
    def tag_delete(request: Request, driver_id: str, group_id: str, tag_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_driver(current, driver_id)
            group = _find_tag_group(entry, group_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        original_tags = group.get("tags", [])
        group["tags"] = [t for t in original_tags if t.get("id") != tag_id]
        error = _save_full_config(current, request)
        if error is not None:
            # XEDGE-461: an asset still references this tag -- nothing was
            # written to disk, so undo the in-memory removal too (group is
            # mutated in place above) before re-rendering the group's own
            # page with the error, rather than showing the tag as gone.
            group["tags"] = original_tags
            type_schema = driver_type_schema(entry["type"])
            group_schema = type_schema["properties"]["tag_groups"]["items"]
            fields = build_object_fields(group_schema, group, skip=_SKIP_ID_AND_TAGS)
            return render(
                request,
                "tag_group_edit.html",
                {
                    "driver_id": driver_id,
                    "group_id": group_id,
                    "fields": fields,
                    "tags": group.get("tags", []),
                    "error": error,
                },
            )
        return RedirectResponse(
            f"/ui/config/drivers/{driver_id}/tag-groups/{group_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # ---- Assets (Sprint C6, XEDGE-460..465; ADR-010) ----
    #
    # A metadata/grouping layer over the driver-first model above, per the
    # project's own resolution of open item Q-3: parameters *reference*
    # existing tags (a <select> of all_tag_refs()'s output, not free text)
    # rather than an asset-first flow that also creates the backing
    # driver/tag entries. Referential integrity beyond "does this option
    # exist in the dropdown" is enforced regardless, in
    # ConfigValidator.validate() (xedge.core.assets.validate_asset_references)
    # -- the Advanced/raw-YAML editor bypasses the dropdown entirely, so
    # that check must not depend on this form having been used.

    asset_entry_schema = core_schema["properties"]["assets"]["items"]

    @router.get("/assets/new", response_class=HTMLResponse)
    def asset_new_form(request: Request) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        return render(
            request, "asset_new.html", {"error": None, "id_value": "", "name_value": ""}
        )

    @router.post("/assets/new", response_class=HTMLResponse)
    def asset_new_submit(
        request: Request, id: str = Form(...), name: str = Form(...)
    ) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        assets = current.setdefault("assets", [])
        if any(a.get("id") == id for a in assets):
            return render(
                request,
                "asset_new.html",
                {
                    "error": f"An asset with id {id!r} already exists",
                    "id_value": id,
                    "name_value": name,
                },
            )
        assets.append({"id": id, "name": name, "enabled": True, "parameters": []})
        error = _save_full_config(current, request)
        if error is not None:
            assets.pop()
            return render(
                request, "asset_new.html", {"error": error, "id_value": id, "name_value": name}
            )
        return RedirectResponse(f"/ui/config/assets/{id}", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/assets/{asset_id}", response_class=HTMLResponse)
    def asset_form(request: Request, asset_id: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_asset(current, asset_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        fields = build_object_fields(asset_entry_schema, entry, skip=_SKIP_ID_AND_PARAMETERS)
        return render(
            request,
            "asset_edit.html",
            {
                "asset_id": asset_id,
                "fields": fields,
                "parameters": entry.get("parameters", []),
            },
        )

    @router.post("/assets/{asset_id}", response_class=HTMLResponse)
    async def asset_save(request: Request, asset_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_asset(current, asset_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})

        raw_form = await request.form()
        form_data: dict[str, str] = {k: v for k, v in raw_form.items() if isinstance(v, str)}
        new_fields = unflatten(form_data, asset_entry_schema, skip=_SKIP_ID_AND_PARAMETERS)
        _replace_editable_fields(entry, asset_entry_schema, new_fields, _SKIP_ID_AND_PARAMETERS)

        error = _save_full_config(current, request)
        fields = build_object_fields(asset_entry_schema, entry, skip=_SKIP_ID_AND_PARAMETERS)
        return render(
            request,
            "asset_edit.html",
            {
                "asset_id": asset_id,
                "fields": fields,
                "parameters": entry.get("parameters", []),
                "error": error,
                "success": error is None,
            },
        )

    @router.post("/assets/{asset_id}/delete")
    def asset_delete(request: Request, asset_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        current["assets"] = [a for a in current.get("assets", []) if a.get("id") != asset_id]
        _save_full_config(current, request)
        return RedirectResponse("/ui/config", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/assets/{asset_id}/parameters/new", response_class=HTMLResponse)
    def asset_parameter_new_form(request: Request, asset_id: str) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_asset(current, asset_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        already_referenced = {p.get("tag_ref") for p in entry.get("parameters", [])}
        available_tag_refs = sorted(all_tag_refs(current.get("drivers", [])) - already_referenced)
        return render(
            request,
            "asset_parameter_new.html",
            {"asset_id": asset_id, "available_tag_refs": available_tag_refs, "error": None},
        )

    @router.post("/assets/{asset_id}/parameters/new", response_class=HTMLResponse)
    async def asset_parameter_new_submit(request: Request, asset_id: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_asset(current, asset_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})

        raw_form = await request.form()
        form_data = {k: v for k, v in raw_form.items() if isinstance(v, str)}
        tag_ref = form_data.get("tag_ref", "")
        parameters = entry.setdefault("parameters", [])
        if any(p.get("tag_ref") == tag_ref for p in parameters):
            already_referenced = {p.get("tag_ref") for p in parameters}
            available_tag_refs = sorted(
                all_tag_refs(current.get("drivers", [])) - already_referenced
            )
            return render(
                request,
                "asset_parameter_new.html",
                {
                    "asset_id": asset_id,
                    "available_tag_refs": available_tag_refs,
                    "error": f"{tag_ref!r} is already a parameter of this asset",
                },
            )
        parameter: dict[str, Any] = {"tag_ref": tag_ref, "store": "store" in form_data}
        if form_data.get("alias"):
            parameter["alias"] = form_data["alias"]
        if form_data.get("unit"):
            parameter["unit"] = form_data["unit"]
        parameters.append(parameter)

        error = _save_full_config(current, request)
        if error is not None:
            parameters.pop()
            already_referenced = {p.get("tag_ref") for p in parameters}
            available_tag_refs = sorted(
                all_tag_refs(current.get("drivers", [])) - already_referenced
            )
            return render(
                request,
                "asset_parameter_new.html",
                {
                    "asset_id": asset_id,
                    "available_tag_refs": available_tag_refs,
                    "error": error,
                },
            )
        return RedirectResponse(
            f"/ui/config/assets/{asset_id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/assets/{asset_id}/parameters/{tag_ref:path}/delete")
    def asset_parameter_delete(request: Request, asset_id: str, tag_ref: str) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        current = _full_config(config_path)
        try:
            entry = _find_asset(current, asset_id)
        except ConfigNotFoundError as exc:
            return render(request, "config_root.html", {"error": str(exc)})
        entry["parameters"] = [
            p for p in entry.get("parameters", []) if p.get("tag_ref") != tag_ref
        ]
        _save_full_config(current, request)
        return RedirectResponse(
            f"/ui/config/assets/{asset_id}", status_code=status.HTTP_303_SEE_OTHER
        )

    # ---- Advanced: raw YAML (escape hatch) ----

    @router.get("/advanced", response_class=HTMLResponse)
    def advanced_editor(request: Request) -> Response:
        redirect = require_read(request)
        if redirect is not None:
            return redirect
        yaml_text = yaml.safe_dump(_full_config(config_path), sort_keys=False)
        return render(request, "config_advanced.html", {"yaml_text": yaml_text})

    @router.post("/advanced", response_class=HTMLResponse)
    def advanced_editor_submit(request: Request, yaml_text: str = Form(...)) -> Response:
        redirect = require_write(request)
        if redirect is not None:
            return redirect
        try:
            new_config = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            return render(
                request,
                "config_advanced.html",
                {"yaml_text": yaml_text, "error": f"Invalid YAML: {exc}"},
            )
        if not isinstance(new_config, dict):
            return render(
                request,
                "config_advanced.html",
                {"yaml_text": yaml_text, "error": "Config must be a YAML mapping"},
            )
        error = _save_full_config(new_config, request)
        return render(
            request,
            "config_advanced.html",
            {"yaml_text": yaml_text, "error": error, "success": error is None},
        )

    return router


def _merge_preserving_absent_secrets(
    schema: dict[str, Any], prior_value: dict[str, Any], new_value: dict[str, Any]
) -> None:
    """For each x-secret field in `schema` that unflatten() omitted from
    `new_value` (because the form's password input was left blank),
    carry the prior value over — FR-WU-006/007's "blank means unchanged",
    completing the contract unflatten() started (it strips blanks so the
    caller, here, can decide what "unchanged" means). Recurses into nested
    objects (e.g. northbound.mqtt.password lives two levels down from the
    "northbound" section schema being edited)."""
    for prop_name, prop_schema in schema.get("properties", {}).items():
        if prop_schema.get("type") == "object":
            nested_prior = prior_value.get(prop_name)
            if not isinstance(nested_prior, dict):
                continue
            nested_new = new_value.get(prop_name)
            if not isinstance(nested_new, dict):
                continue
            _merge_preserving_absent_secrets(prop_schema, nested_prior, nested_new)
            continue
        if prop_schema.get("x-secret") and prop_name not in new_value and prop_name in prior_value:
            new_value[prop_name] = prior_value[prop_name]


def _replace_editable_fields(
    target: dict[str, Any],
    schema: dict[str, Any],
    new_fields: dict[str, Any],
    skip: frozenset[str],
) -> None:
    """Replace target's editable fields (schema's properties minus `skip`)
    with `new_fields`, in place. Unlike target.update(new_fields) alone,
    this also removes a field the operator cleared in the form (unflatten
    drops empty-optional fields rather than including them as ""), and
    unlike target.clear() it never touches keys outside the schema/skip
    (e.g. "id"), which a save must never lose just because the client
    didn't resubmit it."""
    for prop_name in schema.get("properties", {}):
        if prop_name not in skip:
            target.pop(prop_name, None)
    target.update(new_fields)
