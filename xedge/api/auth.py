"""Web UI / REST API auth: multi-user accounts with roles (Sprint 14,
XEDGE-111/112 — HLR §4.9). Supersedes the Sprint 3.5 single-account model
(ADR-007): first-login setup still creates one account with zero friction
(no username prompt — it's always named "admin"), but the store now holds
any number of named users, each with a role from
`xedge.api.permissions.ROLE_PERMISSIONS`. An old, pre-Sprint-14 single-
account file is migrated transparently on first read — same bcrypt hash,
promoted to the `admin` role, no password re-hash and no forced re-setup.

Session model is still a signed, HttpOnly, SameSite=Strict cookie, not a
JWT (`xedge.api.server`'s deferred XEDGE-113 note) — the identity a JWT's
claims would carry is now embedded directly in the signed cookie payload
instead (see `SessionManager`), which is what RBAC needs to resolve a
role, without the claims/expiry/revocation-list machinery a JWT would add
for the API clients (fleet agent, scripts) that don't exist in this
codebase yet.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import time
from pathlib import Path

import bcrypt
from starlette.requests import HTTPConnection

from xedge.api.permissions import ROLE_PERMISSIONS

_BCRYPT_COST = 12
SESSION_COOKIE_NAME = "xedge_session"
DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60
DEFAULT_LOCKOUT_THRESHOLD = 5
DEFAULT_LOCKOUT_WINDOW_SECONDS = 15 * 60
DEFAULT_ROLE = "admin"
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class AuthError(Exception):
    """Raised for auth operations attempted in the wrong state (e.g.
    creating an account that already exists, an unknown role, or deleting
    the last remaining admin)."""


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(_BCRYPT_COST)).decode("ascii")


class UserStore:
    """Named user accounts with roles, persisted as one JSON file:
    `{"users": {"<username>": {"password_hash": ..., "role": ...}}}`."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        """True once at least one account exists — unchanged meaning from
        the single-account model, so first-login-setup gating elsewhere
        (`xedge.api.ui`/`xedge.api.server`) needs no changes."""
        return bool(self._load_users())

    def create(self, password: str) -> None:
        """First-login setup (ADR-007): always creates the initial account
        named "admin" with the `admin` role — no username prompt, so every
        existing setup call site/test (`{"password": ...}`) keeps working
        verbatim."""
        if self.exists():
            raise AuthError("An account already exists; use setup only once")
        self.create_user(DEFAULT_ROLE, password, DEFAULT_ROLE)

    def create_user(self, username: str, password: str, role: str) -> None:
        if role not in ROLE_PERMISSIONS:
            raise AuthError(f"Unknown role: {role!r}")
        if not _USERNAME_PATTERN.fullmatch(username):
            raise AuthError(f"Invalid username: {username!r}")
        users = self._load_users()
        if username in users:
            raise AuthError(f"User {username!r} already exists")
        users[username] = {"password_hash": _hash_password(password), "role": role}
        self._save_users(users)

    def verify(self, username: str, password: str) -> bool:
        entry = self._load_users().get(username)
        if entry is None:
            return False
        return bcrypt.checkpw(password.encode("utf-8"), entry["password_hash"].encode("ascii"))

    def get_role(self, username: str) -> str | None:
        entry = self._load_users().get(username)
        return entry["role"] if entry else None

    def list_users(self) -> list[dict[str, str]]:
        return [
            {"username": username, "role": entry["role"]}
            for username, entry in sorted(self._load_users().items())
        ]

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLE_PERMISSIONS:
            raise AuthError(f"Unknown role: {role!r}")
        users = self._load_users()
        if username not in users:
            raise AuthError(f"No such user: {username!r}")
        users[username]["role"] = role
        self._save_users(users)

    def delete_user(self, username: str) -> None:
        users = self._load_users()
        if username not in users:
            raise AuthError(f"No such user: {username!r}")
        other_admins_remain = any(
            name != username and entry["role"] == "admin" for name, entry in users.items()
        )
        if users[username]["role"] == "admin" and not other_admins_remain:
            raise AuthError("Cannot delete the last remaining admin account")
        del users[username]
        self._save_users(users)

    def change_password(self, username: str, new_password: str) -> None:
        users = self._load_users()
        if username not in users:
            raise AuthError(f"No such user: {username!r}")
        users[username]["password_hash"] = _hash_password(new_password)
        self._save_users(users)

    def _load_users(self) -> dict[str, dict[str, str]]:
        if not self._path.is_file():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if "users" not in raw:
            # Pre-Sprint-14 single-account shape ({"password_hash": "..."})
            # — migrate transparently: same hash (no re-hash), promoted to
            # `admin`, no forced re-setup.
            migrated = {
                "users": {DEFAULT_ROLE: {"password_hash": raw["password_hash"], "role": DEFAULT_ROLE}}
            }
            self._path.write_text(json.dumps(migrated), encoding="utf-8")
            return migrated["users"]
        return raw["users"]

    def _save_users(self, users: dict[str, dict[str, str]]) -> None:
        self._path.write_text(json.dumps({"users": users}), encoding="utf-8")


class SessionManager:
    """Signed, stateless session tokens with a sliding idle timeout
    (FR-WU-007) — still no revocation list; "logging out" just means the
    client stops presenting the cookie. The signed payload now carries the
    username (`f"{username}|{issued_at}"`) rather than being anonymous, so
    a valid session can resolve to a role (RBAC) — embedding it in the
    signed token, rather than a separate in-memory `token -> username` map,
    keeps sessions surviving a process restart exactly as before (validity
    only ever depends on the persisted HMAC secret key, never server-held
    state)."""

    def __init__(
        self, secret_key: bytes, idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    ) -> None:
        self._secret_key = secret_key
        self._idle_timeout_seconds = idle_timeout_seconds

    def issue(self, username: str) -> str:
        """Issue a fresh token for `username`, timestamped now. Call again
        on every authenticated request to implement the *idle* (sliding)
        timeout — see `refresh_if_valid`."""
        payload = f"{username}|{repr(time.time())}"
        signature = self._sign(payload)
        return f"{payload}|{signature}"

    def refresh_if_valid(self, token: str | None) -> tuple[str, str] | None:
        """Validate `token`; if valid, return `(new_token, username)` — a
        freshly-issued replacement token (sliding idle timeout) plus the
        session's username. Returns None if invalid/expired/tampered."""
        username = self._validate(token)
        if username is None:
            return None
        return self.issue(username), username

    def _validate(self, token: str | None) -> str | None:
        if not token or token.count("|") != 2:
            return None
        payload, _, signature = token.rpartition("|")
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        username, _, issued_at_str = payload.partition("|")
        if not username:
            return None
        try:
            issued_at = float(issued_at_str)
        except ValueError:
            return None
        if (time.time() - issued_at) > self._idle_timeout_seconds:
            return None
        return username

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret_key, payload.encode("utf-8"), "sha256").hexdigest()


def resolve_session(
    request: HTTPConnection, session_manager: SessionManager
) -> tuple[str, str] | None:
    """Shared session resolver (`(new_token, username)` or `None`) used by
    every auth-check call site — `xedge.api.server`'s `require_permission`,
    `xedge.api.ui`/`xedge.api.config_ui`'s `require_permission_redirect`, and
    `xedge.api.diagnostics`'s WebSocket handler (Sprint 17) — instead of each
    re-deriving cookie/HMAC logic independently. `HTTPConnection` (not
    `Request`) since it's the common base both `Request` and `WebSocket`
    share — both expose `.cookies` identically, and the diagnostic
    WebSocket needs the exact same resolution this function already does."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return session_manager.refresh_if_valid(token)


class LoginAttemptTracker:
    """In-memory, system-wide failed-login lockout (FR-WU-007) — not
    per-user; state resets on process restart. Kept system-wide rather
    than per-account for simplicity (Sprint 14 adds multi-user accounts,
    not per-account lockout tracking) — if anything more conservative than
    per-user, since it locks out every login attempt after N system-wide
    failures rather than isolating a single targeted account."""

    def __init__(
        self,
        threshold: int = DEFAULT_LOCKOUT_THRESHOLD,
        window_seconds: float = DEFAULT_LOCKOUT_WINDOW_SECONDS,
    ) -> None:
        self._threshold = threshold
        self._window_seconds = window_seconds
        self._failure_times: list[float] = []

    def record_failure(self) -> None:
        now = time.time()
        self._prune(now)
        self._failure_times.append(now)

    def record_success(self) -> None:
        self._failure_times.clear()

    def is_locked_out(self) -> bool:
        self._prune(time.time())
        return len(self._failure_times) >= self._threshold

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        self._failure_times = [t for t in self._failure_times if t > cutoff]


def load_or_create_secret_key(path: Path) -> bytes:
    """Load the session-signing secret key from disk, generating a new
    random one on first run. Persisted so sessions survive a restart."""
    if path.is_file():
        return bytes.fromhex(path.read_text(encoding="utf-8").strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_text(key.hex(), encoding="utf-8")
    return key
