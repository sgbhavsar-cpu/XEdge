"""Fleet Manager device REST API (Sprint C4, XEDGE-442/443; ADR-013 §3):
the post-enrollment half of the manager — heartbeat and certificate
rotation. Served by `xedge.fleet.manager_cli` on a *separate port* from
`xedge.fleet.manager_app`, with `ssl_cert_reqs=CERT_REQUIRED` and
`ssl_ca_certs` pointed at the fleet CA, so the TLS handshake itself refuses
any connection that doesn't present a certificate this CA issued —
enrollment and admin traffic (neither of which has a client certificate)
are on `manager_app`'s port instead, which is why this had to be a second
app rather than a second route on the first.

Note what the mTLS requirement does and doesn't prove: it proves *some*
device this fleet's CA vouched for is calling, cryptographically, before
any request reaches this code. It does not, by itself, prove *which*
device — ASGI/uvicorn have no standard way to hand a route handler the
peer certificate that was presented (verified against this codebase's
actual installed uvicorn, not assumed). `device_token` bearer auth
(unchanged from Sprint 29) still does that identification, so a call here
needs both: a connection the fleet CA vouches for, *and* the specific
device's own token. Defense in depth, not defense in one layer.
"""

from __future__ import annotations

from typing import Any

from cryptography import x509
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from xedge import __version__
from xedge.fleet._device_auth import bearer_value
from xedge.fleet.registry import DeviceRegistry
from xedge.security.ca import CertificateAuthority, InvalidCsrError


class _HeartbeatBody(BaseModel):
    agent_version: str | None = None
    driver_count: int | None = None
    uptime_seconds: float | None = None
    last_config_apply: dict[str, Any] | None = None


class _RotateCertificateBody(BaseModel):
    csr_pem: str


def create_fleet_device_app(
    registry: DeviceRegistry, ca: CertificateAuthority, *, cert_validity_days: int
) -> FastAPI:
    app = FastAPI(title="xEdge Fleet Manager (device)", version=__version__)

    async def require_device_token(device_id: str, authorization: str = Header(default="")) -> None:
        if not await registry.verify_token(device_id, bearer_value(authorization)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing device token")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/fleet/devices/{device_id}/heartbeat")
    async def heartbeat(
        device_id: str, body: _HeartbeatBody, _auth: None = Depends(require_device_token)
    ) -> dict[str, Any]:
        await registry.heartbeat(
            device_id,
            body.agent_version,
            body.driver_count,
            body.uptime_seconds,
            body.last_config_apply,
        )
        pending = await registry.take_pending_config(device_id)
        if pending is None:
            return {"pending_config": None, "pending_config_version": None}
        config, version = pending
        return {"pending_config": config, "pending_config_version": version}

    @app.post("/api/v1/fleet/devices/{device_id}/rotate-certificate")
    async def rotate_certificate(
        device_id: str,
        body: _RotateCertificateBody,
        _auth: None = Depends(require_device_token),
    ) -> dict[str, Any]:
        """XEDGE-443: re-key over the existing mTLS session, before the
        current certificate expires. Deliberately the same signing call as
        initial enrollment (`ca.sign_csr`) — a device that already holds a
        connection this CA vouches for, and can prove its own device_token,
        is exactly as trustworthy as one presenting a fresh join token."""
        try:
            certificate_pem = ca.sign_csr(
                body.csr_pem.encode("ascii"),
                common_name=device_id,
                validity_days=cert_validity_days,
            )
        except (InvalidCsrError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid CSR: {exc}") from exc

        certificate = x509.load_pem_x509_certificate(certificate_pem)
        await registry.record_certificate_issued(
            device_id, certificate.serial_number, certificate.not_valid_after_utc
        )
        return {"certificate_pem": certificate_pem.decode("ascii")}

    return app
