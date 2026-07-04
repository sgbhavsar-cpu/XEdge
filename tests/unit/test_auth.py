from __future__ import annotations

import time
from pathlib import Path

import pytest

from xedge.api.auth import (
    AuthError,
    LoginAttemptTracker,
    SessionManager,
    UserStore,
    load_or_create_secret_key,
)


class TestUserStore:
    def test_no_account_initially(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        assert not store.exists()

    def test_create_then_verify_correct_password(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("hunter2")
        assert store.exists()
        assert store.verify("hunter2") is True

    def test_verify_wrong_password_fails(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("hunter2")
        assert store.verify("wrong") is False

    def test_verify_before_create_fails_safely(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        assert store.verify("anything") is False

    def test_create_twice_raises(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("first")
        with pytest.raises(AuthError):
            store.create("second")

    def test_change_password_before_create_raises(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        with pytest.raises(AuthError):
            store.change_password("new")

    def test_change_password_updates_verification(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("old_password")
        store.change_password("new_password")
        assert store.verify("old_password") is False
        assert store.verify("new_password") is True

    def test_password_hash_never_stored_in_plaintext(self, tmp_path: Path) -> None:
        path = tmp_path / "users.json"
        store = UserStore(path)
        store.create("hunter2")
        assert "hunter2" not in path.read_text(encoding="utf-8")

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "users.json"
        UserStore(path).create("hunter2")
        assert UserStore(path).verify("hunter2") is True


class TestSessionManager:
    def test_issued_token_is_valid(self) -> None:
        manager = SessionManager(secret_key=b"test-secret")
        token = manager.issue()
        assert manager.refresh_if_valid(token) is not None

    def test_tampered_token_is_rejected(self) -> None:
        manager = SessionManager(secret_key=b"test-secret")
        token = manager.issue()
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        assert manager.refresh_if_valid(tampered) is None

    def test_token_signed_with_different_key_is_rejected(self) -> None:
        manager_a = SessionManager(secret_key=b"key-a")
        manager_b = SessionManager(secret_key=b"key-b")
        token = manager_a.issue()
        assert manager_b.refresh_if_valid(token) is None

    def test_expired_token_is_rejected(self) -> None:
        manager = SessionManager(secret_key=b"test-secret", idle_timeout_seconds=0.05)
        token = manager.issue()
        time.sleep(0.1)
        assert manager.refresh_if_valid(token) is None

    def test_refresh_extends_idle_timeout(self) -> None:
        manager = SessionManager(secret_key=b"test-secret", idle_timeout_seconds=0.2)
        token = manager.issue()
        time.sleep(0.12)
        refreshed = manager.refresh_if_valid(token)
        assert refreshed is not None
        time.sleep(0.12)
        # original would have expired by now (0.24s > 0.2s timeout), but the
        # refreshed token was reissued at t=0.12, so it's still within budget
        assert manager.refresh_if_valid(refreshed) is not None

    def test_none_or_malformed_token_rejected(self) -> None:
        manager = SessionManager(secret_key=b"test-secret")
        assert manager.refresh_if_valid(None) is None
        assert manager.refresh_if_valid("not-a-valid-token") is None
        assert manager.refresh_if_valid("") is None


class TestLoginAttemptTracker:
    def test_not_locked_out_initially(self) -> None:
        tracker = LoginAttemptTracker(threshold=5)
        assert tracker.is_locked_out() is False

    def test_locks_out_after_threshold_failures(self) -> None:
        tracker = LoginAttemptTracker(threshold=3)
        for _ in range(3):
            tracker.record_failure()
        assert tracker.is_locked_out() is True

    def test_not_locked_out_below_threshold(self) -> None:
        tracker = LoginAttemptTracker(threshold=5)
        for _ in range(4):
            tracker.record_failure()
        assert tracker.is_locked_out() is False

    def test_success_clears_failure_count(self) -> None:
        tracker = LoginAttemptTracker(threshold=3)
        for _ in range(2):
            tracker.record_failure()
        tracker.record_success()
        tracker.record_failure()
        assert tracker.is_locked_out() is False

    def test_old_failures_outside_window_do_not_count(self) -> None:
        tracker = LoginAttemptTracker(threshold=2, window_seconds=0.05)
        tracker.record_failure()
        time.sleep(0.1)
        tracker.record_failure()
        assert tracker.is_locked_out() is False


class TestSecretKey:
    def test_generates_and_persists_key(self, tmp_path: Path) -> None:
        path = tmp_path / "session_key"
        key1 = load_or_create_secret_key(path)
        assert path.is_file()
        key2 = load_or_create_secret_key(path)
        assert key1 == key2

    def test_generated_key_has_sufficient_entropy(self, tmp_path: Path) -> None:
        key = load_or_create_secret_key(tmp_path / "session_key")
        assert len(key) >= 32
