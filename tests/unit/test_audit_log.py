from __future__ import annotations

import json
from pathlib import Path

from xedge.observability.audit_log import AuditLog


def test_append_and_tail_returns_entries_in_order(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("admin", "auth.login_success")
    log.append("admin", "config.write")
    log.append("bob", "user.created", {"role": "operator"})

    entries = log.tail()
    assert [e["event"] for e in entries] == [
        "auth.login_success",
        "config.write",
        "user.created",
    ]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[2]["details"] == {"role": "operator"}


def test_verify_chain_true_for_untouched_log(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("admin", "auth.login_success")
    log.append("admin", "config.write")
    assert log.verify_chain() is True


def test_verify_chain_false_after_tampering(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("admin", "auth.login_success")
    log.append("admin", "config.write")
    log.append("bob", "user.created")

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["actor"] = "attacker"
    lines[1] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered_log = AuditLog(path)
    assert tampered_log.verify_chain() is False


def test_empty_log_verifies_true(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    assert log.verify_chain() is True
    assert log.tail() == []


def test_tail_filters_by_actor(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("admin", "auth.login_success")
    log.append("bob", "auth.login_success")
    log.append("admin", "config.write")

    entries = log.tail(actor="admin")
    assert [e["event"] for e in entries] == ["auth.login_success", "config.write"]


def test_tail_filters_by_event_prefix(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("admin", "auth.login_success")
    log.append("admin", "config.write")
    log.append("admin", "config.write")

    entries = log.tail(event="config")
    assert len(entries) == 2
    assert all(e["event"] == "config.write" for e in entries)


def test_tail_since_seq_only_returns_newer_entries(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("admin", "a")
    log.append("admin", "b")
    log.append("admin", "c")

    entries = log.tail(since_seq=1)
    assert [e["event"] for e in entries] == ["b", "c"]


def test_tail_limit_keeps_most_recent(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append("admin", f"event{i}")

    entries = log.tail(limit=2)
    assert [e["event"] for e in entries] == ["event3", "event4"]


def test_resuming_from_existing_file_continues_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("admin", "auth.login_success")
    log.append("admin", "config.write")

    resumed_log = AuditLog(path)
    resumed_log.append("bob", "user.created")

    entries = resumed_log.tail()
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert resumed_log.verify_chain() is True


def test_default_actor_and_event_do_not_filter(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("admin", "auth.login_success")
    assert len(log.tail(actor=None, event=None)) == 1
