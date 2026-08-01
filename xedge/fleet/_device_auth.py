"""Bearer-token parsing and hashing shared across `xedge.fleet` modules —
`xedge.fleet.manager_app` (the join-token/admin port),
`xedge.fleet.manager_device_app` (the mTLS device port), and
`xedge.fleet.auth` (Sprint P2, XEDGE-502) all define their own
`require_*` dependency closures inline (matching this module's existing
convention), but header-parsing and the "never persist the secret you
can instead verify a hash of" hashing scheme are identical everywhere a
bearer token shows up (Sprint C4, XEDGE-442), so both live in one place.
"""

from __future__ import annotations

import hashlib


def bearer_value(authorization_header: str) -> str:
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        return ""
    return authorization_header[len(prefix) :]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
