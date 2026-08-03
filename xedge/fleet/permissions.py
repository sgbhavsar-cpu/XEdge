"""RBAC permission matrix for the Fleet Manager (Sprint P2, XEDGE-502) —
mirrors `xedge.api.permissions`'s structure (a fixed `role -> permission
set` dict plus a `has_permission` lookup) and its four role *names*
(readonly/operator/auditor/admin), but not its permission *strings*: the
device-side vocabulary (`tag:read`, `driver:restart`, `ota:trigger`, ...)
describes actions on one gateway's own drivers/tags, none of which the
Fleet Manager's API surface has. This vocabulary describes the actions
the Fleet Manager's API actually exposes instead — device fleet
read/write, user management, and audit history — while keeping the same
admin ⊇ operator ⊇ readonly and admin ⊇ auditor ⊇ readonly nesting shape
the device-side matrix uses.
"""

from __future__ import annotations

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {
            "device:read",
            "device:write",
            "user:manage",
            "audit:read",
        }
    ),
    "operator": frozenset(
        {
            "device:read",
            "device:write",
        }
    ),
    "auditor": frozenset(
        {
            "device:read",
            "audit:read",
        }
    ),
    "readonly": frozenset({"device:read"}),
}


def has_permission(role: str | None, permission: str) -> bool:
    if role is None:
        return False
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
