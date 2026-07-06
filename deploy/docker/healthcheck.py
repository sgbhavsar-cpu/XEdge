"""Docker HEALTHCHECK probe for the xEdge container.

Tries HTTPS first (self-signed-cert tolerant), falls back to plain HTTP —
`tls.enabled` defaults to `true` (Sprint 13), so a stock config serves
HTTPS-only on port 8080, but a `tls.enabled: false` config still needs a
working health check too. Skipping certificate verification here is a
local loopback probe from inside the same container, not the trust
decision a real external client (browser, API caller) makes when it hits
this port from outside.
"""

from __future__ import annotations

import ssl
import sys
import urllib.request

_HEALTH_URL_PATH = "/health"
_PORT = 8080
_TIMEOUT_SECONDS = 3


def main() -> int:
    insecure_context = ssl._create_unverified_context()  # noqa: SLF001 — see module docstring
    try:
        urllib.request.urlopen(
            f"https://127.0.0.1:{_PORT}{_HEALTH_URL_PATH}",
            timeout=_TIMEOUT_SECONDS,
            context=insecure_context,
        )
        return 0
    except Exception:  # noqa: BLE001 — fall through to the plain-HTTP attempt
        pass
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{_PORT}{_HEALTH_URL_PATH}", timeout=_TIMEOUT_SECONDS
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — report and fail the health check
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
