"""Bearer-token parsing shared between `xedge.fleet.manager_app` (the
join-token/admin port) and `xedge.fleet.manager_device_app` (the mTLS
device port) — both define their own `require_device_token`/`require_admin`
dependency closures inline (matching this module's existing convention),
but the header-parsing itself is identical, so it lives in one place
(Sprint C4, XEDGE-442).
"""

from __future__ import annotations


def bearer_value(authorization_header: str) -> str:
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        return ""
    return authorization_header[len(prefix) :]
