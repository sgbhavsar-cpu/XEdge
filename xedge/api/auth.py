"""Web UI / REST API auth: single local user, password set at first login
(ADR-007, HLR §4.9). Deliberately interim scope (FR-WU-008) — no RBAC yet,
the one account can do everything; superseded by the full JWT/RBAC model
planned for Sprint 14 (sprint-planning.md), at which point this account is
promoted to the `admin` role with no password migration needed (same bcrypt
hash format is kept from day one).

Session model is a signed, HttpOnly, SameSite=Strict cookie, not a JWT —
with only one user and one role, there is nothing for JWT's
claims/expiry/revocation-list machinery to buy over a signed cookie.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from pathlib import Path

import bcrypt

_BCRYPT_COST = 12
SESSION_COOKIE_NAME = "xedge_session"
DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60
DEFAULT_LOCKOUT_THRESHOLD = 5
DEFAULT_LOCKOUT_WINDOW_SECONDS = 15 * 60


class AuthError(Exception):
    """Raised for auth operations attempted in the wrong state (e.g.
    creating a second account, or validating against a locked-out login)."""


class UserStore:
    """The single local account (ADR-007). No RBAC: the file on disk
    either holds an account or it doesn't — there's no user list."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self._path.is_file()

    def create(self, password: str) -> None:
        if self.exists():
            raise AuthError("An account already exists; single-user mode allows only one")
        self._write_password_hash(password)

    def verify(self, password: str) -> bool:
        if not self.exists():
            return False
        stored_hash = json.loads(self._path.read_text(encoding="utf-8"))["password_hash"]
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("ascii"))

    def change_password(self, new_password: str) -> None:
        if not self.exists():
            raise AuthError("No account exists yet; use create() for first-login setup")
        self._write_password_hash(new_password)

    def _write_password_hash(self, password: str) -> None:
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(_BCRYPT_COST))
        self._path.write_text(
            json.dumps({"password_hash": password_hash.decode("ascii")}), encoding="utf-8"
        )


class SessionManager:
    """Signed, stateless session tokens with a sliding idle timeout
    (FR-WU-007). No revocation list: with a single account, "logging out"
    just means the client stops presenting the cookie; a stolen cookie's
    blast radius is already bounded by the idle timeout."""

    def __init__(
        self, secret_key: bytes, idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    ) -> None:
        self._secret_key = secret_key
        self._idle_timeout_seconds = idle_timeout_seconds

    def issue(self) -> str:
        """Issue a fresh token, timestamped now. Call again on every
        authenticated request to implement the *idle* (sliding) timeout —
        see `refresh_if_valid`."""
        issued_at = repr(time.time())
        signature = self._sign(issued_at)
        return f"{issued_at}.{signature}"

    def refresh_if_valid(self, token: str | None) -> str | None:
        """Validate `token`; if valid, return a freshly-issued replacement
        token (sliding idle timeout). Returns None if invalid/expired."""
        if not self._is_valid(token):
            return None
        return self.issue()

    def _is_valid(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        issued_at_str, _, signature = token.rpartition(".")
        if not hmac.compare_digest(signature, self._sign(issued_at_str)):
            return False
        try:
            issued_at = float(issued_at_str)
        except ValueError:
            return False
        return (time.time() - issued_at) <= self._idle_timeout_seconds

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret_key, payload.encode("ascii"), "sha256").hexdigest()


class LoginAttemptTracker:
    """In-memory, single-account failed-login lockout (FR-WU-007). State
    resets on process restart — acceptable for the interim single-user
    model, since an attacker able to restart the process already has more
    direct access than the login form would give them."""

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
