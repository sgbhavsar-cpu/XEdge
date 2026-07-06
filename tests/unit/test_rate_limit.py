from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xedge.api.rate_limit import RateLimitMiddleware
from xedge.observability.audit_log import AuditLog


def _build_app(tmp_path: Path, requests_per_minute: int = 5) -> FastAPI:
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware, audit_log=audit_log, requests_per_minute=requests_per_minute
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/thing")
    def thing() -> dict[str, bool]:
        return {"ok": True}

    app.state.audit_log = audit_log
    return app


def test_requests_under_the_limit_pass_through(tmp_path: Path) -> None:
    app = _build_app(tmp_path, requests_per_minute=5)
    client = TestClient(app)
    for _ in range(5):
        assert client.get("/thing").status_code == 200


def test_requests_over_the_limit_return_429_with_retry_after(tmp_path: Path) -> None:
    app = _build_app(tmp_path, requests_per_minute=5)
    client = TestClient(app)
    for _ in range(5):
        client.get("/thing")

    response = client.get("/thing")
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_health_endpoint_is_exempt(tmp_path: Path) -> None:
    app = _build_app(tmp_path, requests_per_minute=2)
    client = TestClient(app)
    for _ in range(10):
        assert client.get("/health").status_code == 200


def test_exceeding_the_limit_is_audit_logged(tmp_path: Path) -> None:
    app = _build_app(tmp_path, requests_per_minute=2)
    client = TestClient(app)
    for _ in range(3):
        client.get("/thing")

    events = [e for e in app.state.audit_log.tail() if e["event"] == "rate_limit.exceeded"]
    assert len(events) == 1
    assert events[0]["details"]["path"] == "/thing"


async def test_different_ips_are_tracked_independently(tmp_path: Path) -> None:
    """TestClient always presents as one client IP, so this test drives the
    middleware's `dispatch()` directly against two synthetic requests with
    different client addresses, rather than reaching into its private
    state."""
    from starlette.requests import Request

    audit_log = AuditLog(tmp_path / "audit.jsonl")

    async def app(scope: dict, receive: object, send: object) -> None:  # pragma: no cover — unused
        raise AssertionError("should not be called directly in this test")

    middleware = RateLimitMiddleware(app, audit_log=audit_log, requests_per_minute=2)

    async def call_next(_request: Request) -> object:
        from starlette.responses import Response

        return Response(status_code=200)

    def _request_from(client_ip: str) -> Request:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/thing",
            "headers": [],
            "client": (client_ip, 12345),
        }
        return Request(scope)

    # Exhaust client A's limit; client B should be unaffected.
    for _ in range(2):
        response = await middleware.dispatch(_request_from("1.2.3.4"), call_next)
        assert response.status_code == 200

    limited = await middleware.dispatch(_request_from("1.2.3.4"), call_next)
    assert limited.status_code == 429

    still_ok = await middleware.dispatch(_request_from("5.6.7.8"), call_next)
    assert still_ok.status_code == 200
