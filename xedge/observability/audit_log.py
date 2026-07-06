"""Durable, hash-chained audit log (Sprint 15, XEDGE-119) — closes the gap
`xedge.api.permissions`' `audit:read` permission and
`security-architecture.md` §2.2/§6.1 ("all authorization decisions are
audit-logged... audit log readable by `auditor` role") point at: the RBAC
work (Sprint 14) only logged permission denials to `structlog` (stdout, or
the capped in-memory `xedge.observability.logging.LogRingBuffer`) — neither
is a durable, queryable, tamper-evident trail.

Append-only JSON Lines on disk, one entry per line (not a single JSON
array — a crash mid-write only ever loses the last incomplete line, never
the whole file): `{seq, timestamp, actor, event, details, prev_hash, hash}`,
hash-chained so tampering with any line breaks every hash after it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_GENESIS_HASH = "0" * 64


def _canonical_json(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._next_seq, self._last_hash = self._resume()

    def _resume(self) -> tuple[int, str]:
        """Seed (next_seq, last_hash) from the existing file's last line,
        if any — so a fresh AuditLog instance continues the same chain
        rather than restarting it (and re-reading the whole file on every
        append, not just once at construction)."""
        if not self._path.is_file():
            return 1, _GENESIS_HASH
        last_line: str | None = None
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last_line = line
        if last_line is None:
            return 1, _GENESIS_HASH
        last_entry = json.loads(last_line)
        return last_entry["seq"] + 1, last_entry["hash"]

    def append(self, actor: str, event: str, details: dict[str, Any] | None = None) -> None:
        with self._lock:
            entry = {
                "seq": self._next_seq,
                "timestamp": datetime.now(UTC).isoformat(),
                "actor": actor,
                "event": event,
                "details": details or {},
                "prev_hash": self._last_hash,
            }
            entry_hash = hashlib.sha256(
                (self._last_hash + _canonical_json(entry)).encode("utf-8")
            ).hexdigest()
            entry["hash"] = entry_hash
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            self._next_seq += 1
            self._last_hash = entry_hash

    def tail(
        self,
        limit: int = 200,
        actor: str | None = None,
        event: str | None = None,
        since_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """Entries with `seq > since_seq`, oldest first, optionally
        filtered by an exact `actor` match or an `event` name prefix (e.g.
        `event="config"` matches `config.write`). `limit` keeps only the
        most recent matches."""
        if not self._path.is_file():
            return []
        matches: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parsed = json.loads(line)
                if parsed["seq"] <= since_seq:
                    continue
                if actor is not None and parsed["actor"] != actor:
                    continue
                if event is not None and not str(parsed["event"]).startswith(event):
                    continue
                matches.append(parsed)
        if limit is not None and len(matches) > limit:
            matches = matches[-limit:]
        return matches

    def verify_chain(self) -> bool:
        """Recompute the hash chain from scratch; True iff every entry's
        `prev_hash`/`hash` still matches what recomputing it would produce —
        false as soon as any line has been altered, removed, or reordered."""
        if not self._path.is_file():
            return True
        expected_prev_hash = _GENESIS_HASH
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry["prev_hash"] != expected_prev_hash:
                    return False
                recomputed = hashlib.sha256(
                    (
                        expected_prev_hash
                        + _canonical_json({k: v for k, v in entry.items() if k != "hash"})
                    ).encode("utf-8")
                ).hexdigest()
                if recomputed != entry["hash"]:
                    return False
                expected_prev_hash = entry["hash"]
        return True
