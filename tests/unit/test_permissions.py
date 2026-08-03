from __future__ import annotations

from xedge.api.permissions import ROLE_PERMISSIONS, has_permission


def test_admin_has_every_permission() -> None:
    assert ROLE_PERMISSIONS["admin"] == {
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


def test_operator_permissions_match_matrix() -> None:
    assert ROLE_PERMISSIONS["operator"] == {
        "tag:read",
        "tag:write",
        "config:read",
        "config:write",
        "driver:restart",
        "northbound:publish",
        "diagnostics:run",
        "alarm:manage",
    }


def test_auditor_permissions_match_matrix() -> None:
    assert ROLE_PERMISSIONS["auditor"] == {"tag:read", "config:read", "audit:read"}


def test_readonly_permissions_match_matrix() -> None:
    assert ROLE_PERMISSIONS["readonly"] == {"tag:read"}


def test_has_permission_true_for_granted_permission() -> None:
    assert has_permission("operator", "config:write") is True


def test_has_permission_false_for_ungranted_permission() -> None:
    assert has_permission("readonly", "config:write") is False


def test_has_permission_false_for_unknown_role() -> None:
    assert has_permission("nonexistent-role", "tag:read") is False


def test_has_permission_false_for_none_role() -> None:
    assert has_permission(None, "tag:read") is False
