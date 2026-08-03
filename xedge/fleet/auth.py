"""Fleet Manager user accounts, sessions, and per-tenant login lockout
(Sprint P2, XEDGE-502) — replaces the single shared `admin_token`
(ADR-009) the same way `xedge.api.auth.UserStore`/`SessionManager`
replaced the device-local Web UI's single-account model in Sprint 14.

Two things deliberately don't carry over from that device-side module:
  - Sessions are opaque, hash-verified bearer tokens stored in
    `fleet_sessions` (see `xedge.fleet.db_models.FleetSession`'s
    docstring for why a stateless, HMAC-signed cookie — right for a
    single device — isn't right for a session that can carry
    `user:manage` over a whole tenant).
  - Login lockout is tracked *per tenant*, not system-wide like the
    device-side `LoginAttemptTracker`: a device has exactly one
    "system"; this manager has many tenants, and a system-wide lockout
    would let one tenant's failed logins deny another tenant's
    legitimate operator — exactly the cross-tenant interference ADR-013
    §5 exists to prevent.

Role permissions themselves are `xedge.fleet.permissions.ROLE_PERMISSIONS`
(mirrors that module's shape, not `xedge.api.permissions`'s vocabulary).

Every method here returns plain dataclasses, never a raw `FleetUser`/
`FleetSession` ORM row — the same "translate at the module boundary"
posture `xedge.fleet.registry` already uses for `DeviceRecord`.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xedge.fleet._device_auth import hash_token
from xedge.fleet.db_models import FleetSession, FleetUser, Tenant
from xedge.fleet.permissions import ROLE_PERMISSIONS

_BCRYPT_COST = 12
DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60
DEFAULT_LOCKOUT_THRESHOLD = 5
DEFAULT_LOCKOUT_WINDOW_SECONDS = 15 * 60
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class AuthError(Exception):
    """Raised for account operations attempted in the wrong state: a
    username already taken in this tenant, an unknown role, or deleting
    the last remaining admin account in a tenant."""


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(_BCRYPT_COST)).decode("ascii")


@dataclass(frozen=True, slots=True)
class FleetUserRecord:
    id: uuid.UUID
    username: str
    role: str


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    tenant_id: str
    username: str
    role: str


class FleetUserStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def resolve_tenant_id(self, tenant_name: str) -> str | None:
        """Login (`POST /auth/login`) takes a human-readable `tenant_name`
        (an operator knows their organization's name, not its internal
        UUID) -- this is the first step of resolving that into the
        `tenant_id` every other method here actually keys on."""
        async with self._sessionmaker() as session:
            result = await session.execute(select(Tenant.id).where(Tenant.name == tenant_name))
            tenant_uuid = result.scalar_one_or_none()
            return str(tenant_uuid) if tenant_uuid is not None else None

    async def create_user(
        self, tenant_id: str, username: str, password: str, role: str
    ) -> FleetUserRecord:
        if role not in ROLE_PERMISSIONS:
            raise AuthError(f"Unknown role: {role!r}")
        if not _USERNAME_PATTERN.fullmatch(username):
            raise AuthError(f"Invalid username: {username!r}")
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session, session.begin():
            existing = await session.execute(
                select(FleetUser.id).where(
                    FleetUser.tenant_id == tenant_uuid, FleetUser.username == username
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise AuthError(f"User {username!r} already exists")
            user = FleetUser(
                tenant_id=tenant_uuid,
                username=username,
                password_hash=_hash_password(password),
                role=role,
                created_at=datetime.now(UTC),
            )
            session.add(user)
            await session.flush()
            return FleetUserRecord(id=user.id, username=user.username, role=user.role)

    async def verify_and_get(
        self, tenant_id: str, username: str, password: str
    ) -> FleetUserRecord | None:
        """Returns the user's record if `password` verifies, else `None`
        -- never distinguishes "no such user" from "wrong password" in
        what it returns, the same anti-enumeration posture
        `DeviceRegistry.consume_join_token` already uses."""
        async with self._sessionmaker() as session:
            user = await self._get(session, tenant_id, username)
            if user is None:
                return None
            if not bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("ascii")):
                return None
            return FleetUserRecord(id=user.id, username=user.username, role=user.role)

    async def has_any_users(self, tenant_id: str) -> bool:
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(FleetUser.id).where(FleetUser.tenant_id == tenant_uuid).limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def list_users(self, tenant_id: str) -> list[FleetUserRecord]:
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(FleetUser)
                .where(FleetUser.tenant_id == tenant_uuid)
                .order_by(FleetUser.username)
            )
            return [
                FleetUserRecord(id=u.id, username=u.username, role=u.role)
                for u in result.scalars().all()
            ]

    async def set_role(self, tenant_id: str, username: str, role: str) -> bool:
        if role not in ROLE_PERMISSIONS:
            raise AuthError(f"Unknown role: {role!r}")
        async with self._sessionmaker() as session, session.begin():
            user = await self._get(session, tenant_id, username)
            if user is None:
                return False
            user.role = role
            return True

    async def change_password(self, tenant_id: str, username: str, new_password: str) -> bool:
        async with self._sessionmaker() as session, session.begin():
            user = await self._get(session, tenant_id, username)
            if user is None:
                return False
            user.password_hash = _hash_password(new_password)
            return True

    async def delete_user(self, tenant_id: str, username: str) -> bool:
        """Returns `False` for "no such user" (matches `set_role`/
        `change_password`'s existing not-found convention) and only ever
        *raises* `AuthError` for "this would delete the tenant's last
        admin" — the API layer needs to tell those two failure reasons
        apart (404 vs. 409), which a single shared exception type
        couldn't do without matching on its message text."""
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session, session.begin():
            user = await self._get(session, tenant_id, username)
            if user is None:
                return False
            if user.role == "admin":
                other_admins = await session.execute(
                    select(FleetUser.id).where(
                        FleetUser.tenant_id == tenant_uuid,
                        FleetUser.id != user.id,
                        FleetUser.role == "admin",
                    )
                )
                if other_admins.first() is None:
                    raise AuthError("Cannot delete the last remaining admin account")
            await session.delete(user)
            return True

    async def _get(self, session: AsyncSession, tenant_id: str, username: str) -> FleetUser | None:
        tenant_uuid = uuid.UUID(tenant_id)
        result = await session.execute(
            select(FleetUser).where(
                FleetUser.tenant_id == tenant_uuid, FleetUser.username == username
            )
        )
        return result.scalar_one_or_none()


class FleetSessionManager:
    def __init__(
        self, engine: AsyncEngine, idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    ) -> None:
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        self._idle_timeout_seconds = idle_timeout_seconds

    async def issue(self, user_id: uuid.UUID) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        async with self._sessionmaker() as session, session.begin():
            session.add(
                FleetSession(
                    token_hash=hash_token(token),
                    user_id=user_id,
                    created_at=now,
                    expires_at=now + timedelta(seconds=self._idle_timeout_seconds),
                )
            )
        return token

    async def resolve_and_refresh(self, token: str) -> AuthenticatedSession | None:
        """Validate `token`; if it's a live session, slide its idle
        timeout forward and return who it belongs to. `role` is read
        fresh from `fleet_users` on every call, not cached on the
        session at login time -- a role change by another admin takes
        effect on this session's very next request, not at its next
        login."""
        now = datetime.now(UTC)
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                select(FleetSession, FleetUser)
                .join(FleetUser, FleetSession.user_id == FleetUser.id)
                .where(FleetSession.token_hash == hash_token(token))
            )
            row = result.first()
            if row is None:
                return None
            session_row, user_row = row
            if session_row.expires_at < now:
                return None
            session_row.expires_at = now + timedelta(seconds=self._idle_timeout_seconds)
            return AuthenticatedSession(
                tenant_id=str(user_row.tenant_id), username=user_row.username, role=user_row.role
            )

    async def revoke(self, token: str) -> None:
        """Logout: delete the session row outright -- the real
        revocation a stateless cookie session can't offer."""
        async with self._sessionmaker() as session, session.begin():
            record = await session.get(FleetSession, hash_token(token))
            if record is not None:
                await session.delete(record)


class LoginLockout:
    """Per-tenant failed-login lockout (mirrors `xedge.api.auth.
    LoginAttemptTracker`'s threshold/window values and logic exactly,
    just keyed by tenant instead of applying system-wide). In-memory,
    like the device-side tracker -- resets on restart, doesn't survive a
    multi-replica deployment. That's an accepted, pre-existing limitation
    carried forward, not a new one."""

    def __init__(
        self,
        threshold: int = DEFAULT_LOCKOUT_THRESHOLD,
        window_seconds: float = DEFAULT_LOCKOUT_WINDOW_SECONDS,
    ) -> None:
        self._threshold = threshold
        self._window_seconds = window_seconds
        self._failure_times: dict[str, list[float]] = defaultdict(list)

    def record_failure(self, tenant_id: str) -> None:
        now = datetime.now(UTC).timestamp()
        self._prune(tenant_id, now)
        self._failure_times[tenant_id].append(now)

    def record_success(self, tenant_id: str) -> None:
        self._failure_times.pop(tenant_id, None)

    def is_locked_out(self, tenant_id: str) -> bool:
        self._prune(tenant_id, datetime.now(UTC).timestamp())
        return len(self._failure_times.get(tenant_id, [])) >= self._threshold

    def _prune(self, tenant_id: str, now: float) -> None:
        cutoff = now - self._window_seconds
        pruned = [t for t in self._failure_times.get(tenant_id, []) if t > cutoff]
        if pruned:
            self._failure_times[tenant_id] = pruned
        else:
            self._failure_times.pop(tenant_id, None)
