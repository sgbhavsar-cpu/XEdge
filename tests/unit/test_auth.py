from __future__ import annotations

import json
import time
from pathlib import Path

import bcrypt
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
        store.create("hunter2hunter2")
        assert store.exists()
        assert store.verify("admin", "hunter2hunter2") is True

    def test_verify_wrong_password_fails(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("hunter2hunter2")
        assert store.verify("admin", "wrong") is False

    def test_verify_before_create_fails_safely(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        assert store.verify("admin", "anything") is False

    def test_create_twice_raises(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("firstpass123")
        with pytest.raises(AuthError):
            store.create("secondpass123")

    def test_change_password_before_create_raises(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        with pytest.raises(AuthError):
            store.change_password("admin", "new")

    def test_change_password_updates_verification(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("old-password-123")
        store.change_password("admin", "new-password-123")
        assert store.verify("admin", "old-password-123") is False
        assert store.verify("admin", "new-password-123") is True

    def test_password_hash_never_stored_in_plaintext(self, tmp_path: Path) -> None:
        path = tmp_path / "users.json"
        store = UserStore(path)
        store.create("hunter2hunter2")
        assert "hunter2hunter2" not in path.read_text(encoding="utf-8")

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "users.json"
        UserStore(path).create("hunter2hunter2")
        assert UserStore(path).verify("admin", "hunter2hunter2") is True

    def test_create_makes_an_admin_account_named_admin(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("hunter2hunter2")
        assert store.get_role("admin") == "admin"


class TestMultiUser:
    def test_create_user_with_role(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        store.create_user("bob", "bobpass123", "operator")
        assert store.get_role("bob") == "operator"
        assert store.verify("bob", "bobpass123")

    def test_create_user_rejects_unknown_role(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        with pytest.raises(AuthError):
            store.create_user("bob", "bobpass123", "not-a-role")

    def test_create_user_rejects_duplicate_username(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        store.create_user("bob", "bobpass123", "operator")
        with pytest.raises(AuthError):
            store.create_user("bob", "different-pass", "readonly")

    def test_create_user_rejects_invalid_username(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        with pytest.raises(AuthError):
            store.create_user("bad username!", "bobpass123", "operator")

    def test_list_users_sorted_by_username(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        store.create_user("zoe", "zoepass123", "readonly")
        store.create_user("bob", "bobpass123", "operator")
        assert store.list_users() == [
            {"username": "admin", "role": "admin"},
            {"username": "bob", "role": "operator"},
            {"username": "zoe", "role": "readonly"},
        ]

    def test_set_role_changes_role(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        store.create_user("bob", "bobpass123", "operator")
        store.set_role("bob", "readonly")
        assert store.get_role("bob") == "readonly"

    def test_set_role_rejects_unknown_role(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        store.create_user("bob", "bobpass123", "operator")
        with pytest.raises(AuthError):
            store.set_role("bob", "not-a-role")

    def test_delete_user_removes_account(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        store.create_user("bob", "bobpass123", "operator")
        store.delete_user("bob")
        assert store.get_role("bob") is None

    def test_delete_last_admin_refused(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        with pytest.raises(AuthError, match="last remaining admin"):
            store.delete_user("admin")

    def test_delete_admin_allowed_when_another_admin_remains(self, tmp_path: Path) -> None:
        store = UserStore(tmp_path / "users.json")
        store.create("adminpass123")
        store.create_user("root2", "root2pass123", "admin")
        store.delete_user("admin")
        assert store.get_role("admin") is None
        assert store.get_role("root2") == "admin"


class TestLegacyMigration:
    def test_pre_sprint14_single_account_file_migrates_transparently(self, tmp_path: Path) -> None:
        path = tmp_path / "users.json"
        old_hash = bcrypt.hashpw(b"legacy-password", bcrypt.gensalt(12)).decode("ascii")
        path.write_text(json.dumps({"password_hash": old_hash}), encoding="utf-8")

        store = UserStore(path)
        assert store.exists()
        assert store.verify("admin", "legacy-password")
        assert store.get_role("admin") == "admin"

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["users"]["admin"]["password_hash"] == old_hash, "hash must not be re-hashed"


class TestSessionManager:
    def test_issued_token_is_valid(self) -> None:
        manager = SessionManager(secret_key=b"test-secret")
        token = manager.issue("admin")
        assert manager.refresh_if_valid(token) is not None

    def test_issue_and_refresh_round_trips_username(self) -> None:
        manager = SessionManager(secret_key=b"test-secret-key")
        token = manager.issue("alice")
        result = manager.refresh_if_valid(token)
        assert result is not None
        new_token, username = result
        assert username == "alice"
        assert new_token != token

    def test_different_users_get_independent_tokens(self) -> None:
        manager = SessionManager(secret_key=b"test-secret-key")
        alice_token = manager.issue("alice")
        bob_token = manager.issue("bob")
        alice_result = manager.refresh_if_valid(alice_token)
        bob_result = manager.refresh_if_valid(bob_token)
        assert alice_result is not None
        assert bob_result is not None
        assert alice_result[1] == "alice"
        assert bob_result[1] == "bob"

    def test_tampered_token_is_rejected(self) -> None:
        manager = SessionManager(secret_key=b"test-secret")
        token = manager.issue("admin")
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        assert manager.refresh_if_valid(tampered) is None

    def test_token_signed_with_different_key_is_rejected(self) -> None:
        manager_a = SessionManager(secret_key=b"key-a")
        manager_b = SessionManager(secret_key=b"key-b")
        token = manager_a.issue("admin")
        assert manager_b.refresh_if_valid(token) is None

    def test_expired_token_is_rejected(self) -> None:
        manager = SessionManager(secret_key=b"test-secret", idle_timeout_seconds=0.05)
        token = manager.issue("admin")
        time.sleep(0.1)
        assert manager.refresh_if_valid(token) is None

    def test_refresh_extends_idle_timeout(self) -> None:
        manager = SessionManager(secret_key=b"test-secret", idle_timeout_seconds=0.2)
        token = manager.issue("admin")
        time.sleep(0.12)
        result = manager.refresh_if_valid(token)
        assert result is not None
        refreshed, _username = result
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
