"""Fleet Manager join-token/admin REST API (Sprint 29, XEDGE-211/213;
reworked Sprint C4, XEDGE-442/444/446; RBAC Sprint P2, XEDGE-502/505;
ADR-013 §3): a separate, standalone service from the per-device xEdge
process — it never imports `xedge.drivers` or `xedge.core.supervisor`
(the actual driver/runtime machinery), only `xedge.fleet.*`,
`xedge.security`, and `xedge.core.config`'s dependency-free
`ConfigValidator` (jsonschema + stdlib only, no further xedge-internal
imports of its own — confirmed by reading that module's own import
list), matching Sprint 32's documented split ("the two share no code" —
read as "no *driver* code," not literally zero symbols from anywhere
under the `xedge.core` namespace).

This is the *unauthenticated-by-certificate* half of the manager: the port
an un-enrolled device reaches to enroll, and an operator/CLI reaches for
everything else. It deliberately does not require a client certificate —
neither exists yet for a device that hasn't enrolled — which is exactly
why it is a separate app/port from `xedge.fleet.manager_device_app`
(post-enrollment device calls, served with `ssl_cert_reqs=CERT_REQUIRED`
against the fleet CA; see `xedge.fleet.manager_cli`).

Auth here: three distinct bearer tokens, never the same value.
  - Join tokens: single-use, time-limited, bound to one `device_id`
    (`DeviceRegistry.create_join_token`/`consume_join_token`) — supersedes
    ADR-009's manager-wide shared secret. An operator provisions one via
    `POST /join-tokens`; the device redeems it via `POST /enroll`.
  - Session tokens: issued by `POST /auth/login` (`tenant_name`,
    `username`, `password`) and presented on every other admin endpoint.
    Sprint P2 (XEDGE-502) replaces the single shared `admin_token`
    (ADR-009) outright with named, per-tenant, role-scoped accounts —
    see `xedge.fleet.auth` for why sessions are opaque, hash-verified,
    revocable tokens rather than `xedge.api.auth.SessionManager`'s
    stateless HMAC-signed cookie. Every route below now takes an
    `AuthenticatedSession` (tenant_id + username + role) from
    `require_permission(...)`, not a bare `tenant_id` from a flat
    secret check.
  - A registered device's own `device_token` (returned by `/enroll`)
    authenticates its calls on the *device* port, not this one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from cryptography import x509
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from xedge import __version__
from xedge.core.config import ConfigValidationError, ConfigValidator
from xedge.fleet._device_auth import bearer_value
from xedge.fleet.audit import FleetAuditLog
from xedge.fleet.auth import (
    AuthenticatedSession,
    AuthError,
    FleetSessionManager,
    FleetUserStore,
    LoginLockout,
)
from xedge.fleet.permissions import has_permission
from xedge.fleet.registry import DeviceRegistry, DeviceTenantConflictError
from xedge.security.ca import CertificateAuthority, InvalidCsrError

_DEFAULT_JOIN_TOKEN_TTL_SECONDS = 3600.0


class _LoginBody(BaseModel):
    tenant_name: str
    username: str
    password: str


class _CreateUserBody(BaseModel):
    username: str
    password: str
    role: str


class _UpdateUserBody(BaseModel):
    role: str | None = None
    password: str | None = None


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
    user_store: FleetUserStore,
    session_manager: FleetSessionManager,
    audit_log: FleetAuditLog,
    login_lockout: LoginLockout,
    *,
    cert_validity_days: int,
    config_validator: ConfigValidator | None = None,
) -> FastAPI:
    app = FastAPI(title="xEdge Fleet Manager", version=__version__)

    async def require_session(authorization: str = Header(default="")) -> AuthenticatedSession:
        token = bearer_value(authorization)
        resolved = await session_manager.resolve_and_refresh(token) if token else None
        if resolved is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
        return resolved

    def require_permission(
        permission: str,
    ) -> Callable[[AuthenticatedSession], Awaitable[AuthenticatedSession]]:
        """Returns a FastAPI dependency: authenticate via `require_session`,
        then require `permission` on top. Two layers, not one, so a
        request with a valid session but the wrong role gets 403 (it *is*
        someone), not 401 (indistinguishable from no session at all)."""

        async def _dependency(
            session: AuthenticatedSession = Depends(require_session),  # noqa: B008
        ) -> AuthenticatedSession:
            if not has_permission(session.role, permission):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, f"Role {session.role!r} lacks {permission!r}"
                )
            return session

        return _dependency

    # Computed once per app instance, not inline in each route's default
    # argument: `Depends(require_permission("..."))` would call `require_
    # permission` at route-definition time either way, but ruff's B008
    # ("no function calls in argument defaults") wants a bare variable
    # reference regardless -- confirmed empirically (not just the usual
    # `fastapi.Depends`/`Query` exemption ruff's own default allowlist
    # normally grants) that this exemption stops applying the moment the
    # `Depends(...)`-defaulted parameter is annotated with anything other
    # than a plain builtin type: swapping `AuthenticatedSession` for a
    # bare `str` return type makes ruff drop the complaint entirely.
    # Every route dependency in this file needs the richer type, so the
    # B008 suppressions below stay -- this is a ruff/flake8-bugbear
    # limitation with this particular combination, not a real "call in a
    # default" hazard (`require_device_read` etc. are plain names by the
    # time any route references them).
    require_device_read = require_permission("device:read")
    require_device_write = require_permission("device:write")
    require_user_manage = require_permission("user:manage")
    require_audit_read = require_permission("audit:read")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/fleet/auth/login")
    async def login(body: _LoginBody) -> dict[str, Any]:
        """XEDGE-502: exchange `tenant_name` + `username` + `password` for
        a bearer session token, presented as `Authorization: Bearer ...`
        on every other admin endpoint. `tenant_name`, not `tenant_id`: an
        operator knows their organization's name, not its internal UUID
        — the same reason `xedge.fleet.registry.DeviceRegistry.
        ensure_default_tenant` bootstraps a human-readable "default"
        rather than only ever minting bare UUIDs. Every failure reason
        (unknown tenant, unknown username, wrong password) returns the
        same 401 with the same message — the same anti-enumeration
        posture already established for join tokens."""
        tenant_id = await user_store.resolve_tenant_id(body.tenant_name)
        if tenant_id is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
        if login_lockout.is_locked_out(tenant_id):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed login attempts")
        user = await user_store.verify_and_get(tenant_id, body.username, body.password)
        if user is None:
            login_lockout.record_failure(tenant_id)
            await audit_log.record(tenant_id, body.username, "auth.login_failure")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
        login_lockout.record_success(tenant_id)
        token = await session_manager.issue(user.id)
        await audit_log.record(tenant_id, user.username, "auth.login_success")
        return {"session_token": token, "username": user.username, "role": user.role}

    @app.post("/api/v1/fleet/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        authorization: str = Header(default=""),
        session: AuthenticatedSession = Depends(require_session),  # noqa: B008
    ) -> None:
        """Sprint P3: the one way a session ends today besides idle-timeout
        expiry — `FleetSessionManager.revoke` existed already but no route
        called it. Requires a currently-valid session (via `require_session`)
        purely so this can be audit-logged as the right actor; `revoke`
        itself would happily no-op on an already-invalid token."""
        await session_manager.revoke(bearer_value(authorization))
        await audit_log.record(session.tenant_id, session.username, "auth.logout")

    @app.post("/api/v1/fleet/users", status_code=status.HTTP_201_CREATED)
    async def create_user(
        body: _CreateUserBody,
        session: AuthenticatedSession = Depends(require_user_manage),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            user = await user_store.create_user(
                session.tenant_id, body.username, body.password, body.role
            )
        except AuthError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        await audit_log.record(
            session.tenant_id,
            session.username,
            "user.created",
            {"username": body.username, "role": body.role},
        )
        return {"username": user.username, "role": user.role}

    @app.get("/api/v1/fleet/users")
    async def list_users(
        session: AuthenticatedSession = Depends(require_user_manage),  # noqa: B008
    ) -> list[dict[str, Any]]:
        return [
            {"username": u.username, "role": u.role}
            for u in await user_store.list_users(session.tenant_id)
        ]

    @app.patch("/api/v1/fleet/users/{username}")
    async def update_user(
        username: str,
        body: _UpdateUserBody,
        session: AuthenticatedSession = Depends(require_user_manage),  # noqa: B008
    ) -> dict[str, str]:
        if body.role is not None:
            try:
                role_updated = await user_store.set_role(session.tenant_id, username, body.role)
            except AuthError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            if not role_updated:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such user: {username!r}")
            await audit_log.record(
                session.tenant_id,
                session.username,
                "user.role_changed",
                {"username": username, "role": body.role},
            )
        if body.password is not None:
            password_updated = await user_store.change_password(
                session.tenant_id, username, body.password
            )
            if not password_updated:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such user: {username!r}")
            await audit_log.record(
                session.tenant_id, session.username, "user.password_changed", {"username": username}
            )
        return {"username": username}

    @app.delete("/api/v1/fleet/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_user(
        username: str,
        session: AuthenticatedSession = Depends(require_user_manage),  # noqa: B008
    ) -> None:
        try:
            deleted = await user_store.delete_user(session.tenant_id, username)
        except AuthError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such user: {username!r}")
        await audit_log.record(
            session.tenant_id, session.username, "user.deleted", {"username": username}
        )

    @app.get("/api/v1/fleet/audit")
    async def get_audit_log(
        since_seq: int | None = None,
        actor: str | None = None,
        event: str | None = None,
        limit: int = 200,
        session: AuthenticatedSession = Depends(require_audit_read),  # noqa: B008
    ) -> list[dict[str, Any]]:
        entries = await audit_log.tail(
            session.tenant_id, since_seq=since_seq, actor=actor, event_prefix=event, limit=limit
        )
        return [
            {
                "id": e.id,
                "created_at": e.created_at.isoformat(),
                "actor": e.actor,
                "event": e.event,
                "details": e.details,
            }
            for e in entries
        ]

    @app.post("/api/v1/fleet/join-tokens")
    async def create_join_token(
        body: _CreateJoinTokenBody,
        session: AuthenticatedSession = Depends(require_device_write),  # noqa: B008
    ) -> dict[str, Any]:
        """XEDGE-442: an operator provisions a one-time enrollment
        credential for a specific device, ahead of that device ever
        contacting the manager. Replaces the old manager-wide `join_token`
        this app used to accept directly."""
        try:
            token = await registry.create_join_token(
                session.tenant_id, body.device_id, body.ttl_seconds
            )
        except DeviceTenantConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        await audit_log.record(
            session.tenant_id, session.username, "join_token.created", {"device_id": body.device_id}
        )
        return {"join_token": token, "device_id": body.device_id, "ttl_seconds": body.ttl_seconds}

    @app.get("/api/v1/fleet/join-tokens")
    async def list_join_tokens(
        session: AuthenticatedSession = Depends(require_device_write),  # noqa: B008
    ) -> list[dict[str, Any]]:
        """XEDGE-513. `id` is the row's `token_hash` — the raw token was
        never persisted (see `xedge.fleet.registry`'s module docstring),
        so it's the only thing this list can identify a row by; it's not
        a secret (revealing a hash isn't the same as revealing the token
        it hashes), unlike the raw token this list can never show again."""
        return [
            {
                "id": t.id,
                "device_id": t.device_id,
                "created_at": t.created_at.isoformat(),
                "expires_at": t.expires_at.isoformat(),
                "consumed_at": t.consumed_at.isoformat() if t.consumed_at else None,
                "revoked_at": t.revoked_at.isoformat() if t.revoked_at else None,
                "revoked_by": t.revoked_by,
                "status": t.status,
            }
            for t in await registry.list_join_tokens(session.tenant_id)
        ]

    @app.delete("/api/v1/fleet/join-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_join_token(
        token_id: str,
        session: AuthenticatedSession = Depends(require_device_write),  # noqa: B008
    ) -> None:
        """XEDGE-513: an operator-initiated kill, distinct from a device
        redeeming the token itself — see `DeviceRegistry.revoke_join_token`.
        Idempotent (revoking twice, or revoking an already-consumed/expired
        token, still succeeds) — 404 means only "no such token in this
        tenant," not "already unusable for some other reason."""
        if not await registry.revoke_join_token(session.tenant_id, token_id, session.username):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such join token: {token_id!r}")
        await audit_log.record(
            session.tenant_id, session.username, "join_token.revoked", {"token_id": token_id}
        )

    @app.post("/api/v1/fleet/enroll")
    async def enroll(body: _EnrollBody) -> dict[str, Any]:
        """XEDGE-442: redeem a single-use join token and a CSR together —
        the token proves this caller is allowed to become `device_id`, the
        CSR supplies the public key that identity's certificate is issued
        for. The private key backing the CSR never reached this process
        (ADR-013 §3); only the certificate is handed back.

        XEDGE-500: the token also carries the `tenant_id` it was
        provisioned under — the device joins *that* tenant. The device
        never asserts its own tenant; only whoever held the join token
        decides it, same trust boundary as `device_id` itself. Not an
        admin action (no operator session involved), so this isn't
        audit-logged the way user/token/config actions below are."""
        tenant_id = await registry.consume_join_token(body.device_id, body.join_token)
        if tenant_id is None:
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

        device_token = await registry.register(
            tenant_id,
            body.device_id,
            body.display_name,
            body.agent_version,
            body.heartbeat_interval_seconds,
        )
        certificate = x509.load_pem_x509_certificate(certificate_pem)
        await registry.record_certificate_issued(
            body.device_id,
            certificate.serial_number,
            certificate.not_valid_before_utc,
            certificate.not_valid_after_utc,
            reason="enrollment",
        )
        return {
            "device_token": device_token,
            "certificate_pem": certificate_pem.decode("ascii"),
            "ca_certificate_pem": ca.certificate_pem.decode("ascii"),
        }

    @app.get("/api/v1/fleet/devices")
    async def list_devices(
        session: AuthenticatedSession = Depends(require_device_read),  # noqa: B008
    ) -> list[dict[str, Any]]:
        return [_device_summary(r) for r in await registry.list_devices(session.tenant_id)]

    @app.get("/api/v1/fleet/devices/{device_id}")
    async def get_device(
        device_id: str,
        session: AuthenticatedSession = Depends(require_device_read),  # noqa: B008
    ) -> dict[str, Any]:
        record = await registry.get(session.tenant_id, device_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return _device_summary(record)

    @app.patch("/api/v1/fleet/devices/{device_id}/metadata")
    async def update_metadata(
        device_id: str,
        body: _UpdateMetadataBody,
        session: AuthenticatedSession = Depends(require_device_write),  # noqa: B008
    ) -> dict[str, Any]:
        """XEDGE-444: gateway provisioning metadata (CRD §4.9) an operator
        knows about the physical device — serial number, make, protocol,
        firmware version — that xEdge's own software has no way to
        discover on its own. Only the fields present in the request body
        are touched; omitted fields keep their current value."""
        fields = body.model_dump(exclude_unset=True)
        if not await registry.update_metadata(session.tenant_id, device_id, fields):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        record = await registry.get(session.tenant_id, device_id)
        if record is None:
            # update_metadata just returned True for this exact (tenant_id,
            # device_id) above -- an `assert` here would be stripped under
            # `python -O` (bandit B101), so this stays a real, unstrippable
            # check even though it's not reachable in practice.
            raise AssertionError(f"{device_id!r} vanished between update and read-back")
        return _device_summary(record)

    @app.post("/api/v1/fleet/devices/{device_id}/config", status_code=status.HTTP_202_ACCEPTED)
    async def push_config(
        device_id: str,
        body: _ConfigPushBody,
        session: AuthenticatedSession = Depends(require_device_write),  # noqa: B008
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
            version = await registry.queue_config(
                session.tenant_id, device_id, body.config, session.username
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        await audit_log.record(
            session.tenant_id,
            session.username,
            "config.push",
            {"device_id": device_id, "pending_config_version": version},
        )
        return {"queued": True, "pending_config_version": version}

    @app.get("/api/v1/fleet/devices/{device_id}/config/status")
    async def config_status(
        device_id: str,
        session: AuthenticatedSession = Depends(require_device_read),  # noqa: B008
    ) -> dict[str, Any]:
        record = await registry.get(session.tenant_id, device_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return {
            "has_pending_config": record.has_pending_config,
            "pending_config_version": record.pending_config_version,
            "last_config_apply": record.last_config_apply,
        }

    @app.get("/api/v1/fleet/devices/{device_id}/config-history")
    async def get_config_history(
        device_id: str,
        session: AuthenticatedSession = Depends(require_device_read),  # noqa: B008
    ) -> list[dict[str, Any]]:
        """XEDGE-512: the append-only record of every config ever pushed
        to this device — `config/status` above shows only the current
        queue/latest-apply snapshot."""
        if await registry.get(session.tenant_id, device_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return [
            {
                "config_version": h.config_version,
                "config": h.config,
                "pushed_at": h.pushed_at.isoformat(),
                "pushed_by": h.pushed_by,
                "applied_at": h.applied_at.isoformat() if h.applied_at else None,
                "apply_success": h.apply_success,
                "apply_error": h.apply_error,
            }
            for h in await registry.list_config_history(session.tenant_id, device_id)
        ]

    @app.get("/api/v1/fleet/devices/{device_id}/certificate-history")
    async def get_certificate_history(
        device_id: str,
        session: AuthenticatedSession = Depends(require_device_read),  # noqa: B008
    ) -> list[dict[str, Any]]:
        """XEDGE-512: every certificate this device has ever been issued —
        initial enrollment plus every later rotation — distinct from
        `cert_serial_number`/`cert_not_after` on the device summary above,
        which hold only the current certificate's identity."""
        if await registry.get(session.tenant_id, device_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return [
            {
                "serial_number": h.serial_number,
                "not_before": h.not_before.isoformat(),
                "not_after": h.not_after.isoformat(),
                "issued_at": h.issued_at.isoformat(),
                "reason": h.reason,
            }
            for h in await registry.list_certificate_history(session.tenant_id, device_id)
        ]

    return app
