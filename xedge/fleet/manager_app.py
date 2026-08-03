"""Fleet Manager join-token/admin REST API (Sprint 29, XEDGE-211/213;
reworked Sprint C4, XEDGE-442/444/446; ADR-013 §3): a separate, standalone
service from the per-device xEdge process — it never imports
`xedge.drivers` or `xedge.core.supervisor` (the actual driver/runtime
machinery), only `xedge.fleet.registry`, `xedge.security`, and
`xedge.core.config`'s dependency-free `ConfigValidator` (jsonschema +
stdlib only, no further xedge-internal imports of its own — confirmed by
reading that module's own import list), matching Sprint 32's documented
split ("the two share no code" — read as "no *driver* code," not literally
zero symbols from anywhere under the `xedge.core` namespace).

This is the *unauthenticated-by-certificate* half of the manager: the port
an un-enrolled device reaches to enroll, and an operator/CLI reaches for
everything else. It deliberately does not require a client certificate —
neither exists yet for a device that hasn't enrolled — which is exactly
why it is a separate app/port from `xedge.fleet.manager_device_app`
(post-enrollment device calls, served with `ssl_cert_reqs=CERT_REQUIRED`
against the fleet CA; see `xedge.fleet.manager_cli`).

Auth here: two distinct bearer tokens, never the same value.
  - Join tokens: single-use, time-limited, bound to one `device_id`
    (`DeviceRegistry.create_join_token`/`consume_join_token`) — supersedes
    ADR-009's manager-wide shared secret. An operator provisions one via
    `POST /join-tokens`; the device redeems it via `POST /enroll`.
  - `admin_token`: presented by an operator/CLI for every other endpoint
    (create join tokens, list/inspect devices, push a config). A plain
    shared secret (ADR-009) — no RBAC/multi-operator model yet, mirroring
    how the per-device Web UI started as a single account before Sprint 14
    added RBAC.
  - A registered device's own `device_token` (returned by `/enroll`)
    authenticates its calls on the *device* port, not this one.
"""

from __future__ import annotations

import hmac
from typing import Any

from cryptography import x509
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from xedge import __version__
from xedge.core.config import ConfigValidationError, ConfigValidator
from xedge.fleet._device_auth import bearer_value
from xedge.fleet.registry import DeviceRegistry
from xedge.security.ca import CertificateAuthority, InvalidCsrError

_DEFAULT_JOIN_TOKEN_TTL_SECONDS = 3600.0


class _CreateJoinTokenBody(BaseModel):
    device_id: str
    ttl_seconds: float = _DEFAULT_JOIN_TOKEN_TTL_SECONDS


class _EnrollBody(BaseModel):
    device_id: str
    join_token: str
    csr_pem: str
    display_name: str | None = None
    agent_version: str | None = None
    heartbeat_interval_seconds: float = 60


class _ConfigPushBody(BaseModel):
    config: dict[str, Any]


class _UpdateMetadataBody(BaseModel):
    """All fields optional and unset by default (Sprint C4, XEDGE-444) —
    `model_dump(exclude_unset=True)` is how the endpoint below tells
    `DeviceRegistry.update_metadata` "the operator didn't mention this
    field" apart from "the operator explicitly set this field to null,"
    which a bare default of `None` on every field couldn't distinguish."""

    display_name: str | None = None
    serial_number: str | None = None
    make: str | None = None
    protocol: str | None = None
    hardware_firmware_version: str | None = None


def _device_summary(record: Any) -> dict[str, Any]:
    return {
        "device_id": record.device_id,
        "display_name": record.display_name,
        "status": record.status,
        "connection_state": record.connection_state.value,
        "registered_at": record.registered_at.isoformat(),
        "agent_version": record.agent_version,
        "last_seen_at": record.last_seen_at.isoformat() if record.last_seen_at else None,
        "driver_count": record.driver_count,
        "uptime_seconds": record.uptime_seconds,
        "last_config_apply": record.last_config_apply,
        "has_pending_config": record.has_pending_config,
        "pending_config_version": record.pending_config_version,
        "cert_serial_number": record.cert_serial_number,
        "cert_not_after": record.cert_not_after.isoformat() if record.cert_not_after else None,
        "serial_number": record.serial_number,
        "make": record.make,
        "protocol": record.protocol,
        "hardware_firmware_version": record.hardware_firmware_version,
    }


def create_fleet_manager_app(
    registry: DeviceRegistry,
    ca: CertificateAuthority,
    *,
    admin_token: str,
    cert_validity_days: int,
    config_validator: ConfigValidator | None = None,
) -> FastAPI:
    app = FastAPI(title="xEdge Fleet Manager", version=__version__)

    def require_admin(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(bearer_value(authorization), admin_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing admin token")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/fleet/join-tokens")
    def create_join_token(
        body: _CreateJoinTokenBody, _auth: None = Depends(require_admin)
    ) -> dict[str, Any]:
        """XEDGE-442: an operator provisions a one-time enrollment
        credential for a specific device, ahead of that device ever
        contacting the manager. Replaces the old manager-wide `join_token`
        this app used to accept directly."""
        token = registry.create_join_token(body.device_id, body.ttl_seconds)
        return {"join_token": token, "device_id": body.device_id, "ttl_seconds": body.ttl_seconds}

    @app.post("/api/v1/fleet/enroll")
    def enroll(body: _EnrollBody) -> dict[str, Any]:
        """XEDGE-442: redeem a single-use join token and a CSR together —
        the token proves this caller is allowed to become `device_id`, the
        CSR supplies the public key that identity's certificate is issued
        for. The private key backing the CSR never reached this process
        (ADR-013 §3); only the certificate is handed back."""
        if not registry.consume_join_token(body.device_id, body.join_token):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Invalid, expired, or used join token"
            )
        try:
            certificate_pem = ca.sign_csr(
                body.csr_pem.encode("ascii"),
                common_name=body.device_id,
                validity_days=cert_validity_days,
            )
        except (InvalidCsrError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid CSR: {exc}") from exc

        device_token = registry.register(
            body.device_id,
            body.display_name,
            body.agent_version,
            body.heartbeat_interval_seconds,
        )
        certificate = x509.load_pem_x509_certificate(certificate_pem)
        registry.record_certificate_issued(
            body.device_id, certificate.serial_number, certificate.not_valid_after_utc
        )
        return {
            "device_token": device_token,
            "certificate_pem": certificate_pem.decode("ascii"),
            "ca_certificate_pem": ca.certificate_pem.decode("ascii"),
        }

    @app.get("/api/v1/fleet/devices")
    def list_devices(_auth: None = Depends(require_admin)) -> list[dict[str, Any]]:
        return [_device_summary(r) for r in registry.list_devices()]

    @app.get("/api/v1/fleet/devices/{device_id}")
    def get_device(device_id: str, _auth: None = Depends(require_admin)) -> dict[str, Any]:
        record = registry.get(device_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return _device_summary(record)

    @app.patch("/api/v1/fleet/devices/{device_id}/metadata")
    def update_metadata(
        device_id: str, body: _UpdateMetadataBody, _auth: None = Depends(require_admin)
    ) -> dict[str, Any]:
        """XEDGE-444: gateway provisioning metadata (CRD §4.9) an operator
        knows about the physical device — serial number, make, protocol,
        firmware version — that xEdge's own software has no way to
        discover on its own. Only the fields present in the request body
        are touched; omitted fields keep their current value."""
        fields = body.model_dump(exclude_unset=True)
        if not registry.update_metadata(device_id, fields):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return _device_summary(registry.get(device_id))

    @app.post("/api/v1/fleet/devices/{device_id}/config", status_code=status.HTTP_202_ACCEPTED)
    def push_config(
        device_id: str, body: _ConfigPushBody, _auth: None = Depends(require_admin)
    ) -> dict[str, Any]:
        """Queues `config` for delivery on the device's next heartbeat
        (XEDGE-213) — not applied synchronously; see ADR-009 for why this
        is a pull, not a push, despite the story title. Returns 202, not
        200: "accepted for delivery," matching the async reality.

        XEDGE-446: validated against the core config schema *here*, not
        only on the device — an operator authoring a config finds out
        immediately, in this response, rather than only on the device's
        next heartbeat report (`last_config_apply.success = False`,
        potentially minutes later). `config_validator` is optional only
        because not every caller constructing this app (chiefly tests
        that don't care about this concern) wants to supply one;
        `xedge.fleet.manager_cli` always does."""
        if config_validator is not None:
            try:
                config_validator.validate(body.config)
            except ConfigValidationError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        try:
            version = registry.queue_config(device_id, body.config)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        return {"queued": True, "pending_config_version": version}

    @app.get("/api/v1/fleet/devices/{device_id}/config/status")
    def config_status(device_id: str, _auth: None = Depends(require_admin)) -> dict[str, Any]:
        record = registry.get(device_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return {
            "has_pending_config": record.has_pending_config,
            "pending_config_version": record.pending_config_version,
            "last_config_apply": record.last_config_apply,
        }

    return app
