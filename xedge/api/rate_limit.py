"""Per-client-IP request rate limiting (Sprint 18, XEDGE-144/285 — IEC
62443 SR 7.1, "Denial of service protection"). Covers the REST API and
every Web UI route identically (same app, same middleware) — XEDGE-285
explicitly calls out the Web UI as no lower-risk a surface than the REST
API, since it's the same authenticated, write-capable endpoints wrapped in
HTML.

In-memory sliding window, same shape as `xedge.api.auth.LoginAttemptTracker`
(a `list[float]` of hit timestamps pruned to the current window) — no new
dependency justified for something this small.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from xedge.observability.audit_log import AuditLog

_WINDOW_SECONDS = 60.0
_DEFAULT_REQUESTS_PER_MINUTE = 100
# Crude, bounded safety valve against unbounded per-IP dict growth under a
# distributed scan/flood (SR 7.2, resource management) — this device binds
# loopback-only by default, so the realistic exposure is low, but a hard
# cap costs nothing and bounds worst-case memory regardless.
_MAX_TRACKED_IPS = 10_000


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: object,
        *,
        audit_log: AuditLog,
        requests_per_minute: int = _DEFAULT_REQUESTS_PER_MINUTE,
        exempt_paths: frozenset[str] = frozenset({"/health"}),
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._audit_log = audit_log
        self._requests_per_minute = requests_per_minute
        self._exempt_paths = exempt_paths
        self._hits: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        cutoff = now - _WINDOW_SECONDS

        if len(self._hits) > _MAX_TRACKED_IPS:
            self._hits.clear()

        hits = [t for t in self._hits.get(client_ip, []) if t > cutoff]
        if len(hits) >= self._requests_per_minute:
            self._audit_log.append(client_ip, "rate_limit.exceeded", {"path": request.url.path})
            return Response(
                content="Too Many Requests",
                status_code=429,
                headers={"Retry-After": str(int(_WINDOW_SECONDS))},
            )

        hits.append(now)
        self._hits[client_ip] = hits
        return await call_next(request)
