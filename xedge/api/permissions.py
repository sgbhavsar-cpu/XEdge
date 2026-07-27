"""RBAC permission matrix (Sprint 14, XEDGE-111) — the exact matrix
documented in docs/architecture/security-architecture.md §2.2. Custom roles
(the doc allows "any combination of the above permissions") aren't
supported yet; these four fixed roles are the v1 scope.
"""

from __future__ import annotations

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {
            "tag:read",
            "tag:write",
            "config:read",
            "config:write",
            "driver:restart",
            "northbound:publish",
            "security:manage",
            "user:manage",
            "audit:read",
            "ota:trigger",
            "diagnostics:run",
            "alarm:manage",
        }
    ),
    "operator": frozenset(
        {
            "tag:read",
            "tag:write",
            "config:read",
            "config:write",
            "driver:restart",
            "northbound:publish",
            "diagnostics:run",
            "alarm:manage",
        }
    ),
    "auditor": frozenset({"tag:read", "config:read", "audit:read"}),
    "readonly": frozenset({"tag:read"}),
}


def has_permission(role: str | None, permission: str) -> bool:
    if role is None:
        return False
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
