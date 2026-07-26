"""Structured JSON logging setup (structlog), per FR-DF-006 / NFR-M / SR-DS-003.

Renders one JSON object per line to stdout, suitable for capture by the
Docker/systemd journal and forwarding to a SIEM via Fluentd or syslog-ng.
"""

from __future__ import annotations

import logging
import sys
from collections import deque
from collections.abc import Mapping
from typing import Any

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, WrappedLogger

_CONFIGURED = False

_DEFAULT_LOG_BUFFER_SIZE = 2000


class LogRingBuffer:
    """Bounded buffer of recent structured log events (FR-WU-* log viewer).

    Each entry is the structlog event dict as it stood right before
    JSONRenderer ran it (`_capture_to_ring_buffer` sits just before
    JSONRenderer in the processor chain) — TimeStamper, add_log_level, and
    format_exc_info have already turned it into JSON-safe plain types, so
    entries can be handed straight to a JSON API response.

    Tagged with a monotonically increasing `seq` so pollers can ask for
    `seq > since_seq` and only get what they haven't seen yet — the same
    incremental-fetch shape the rest of the Web UI already polls with
    (xedge.api.static.xedge-ui.js).
    """

    def __init__(self, max_size: int = _DEFAULT_LOG_BUFFER_SIZE) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_size)
        self._next_seq = 1

    def append(self, event_dict: Mapping[str, Any]) -> None:
        entry = dict(event_dict)
        entry["seq"] = self._next_seq
        self._next_seq += 1
        self._entries.append(entry)

    def tail(
        self,
        since_seq: int = 0,
        instance_id: str | None = None,
        source: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Entries with `seq > since_seq`, oldest first, optionally filtered
        by an exact `instance_id` match or an `event` name prefix (e.g.
        `source="northbound"` matches events like `northbound.connected`).
        `limit`, if given, keeps only the most recent matches."""
        matches = [
            entry
            for entry in self._entries
            if entry["seq"] > since_seq
            and (instance_id is None or entry.get("instance_id") == instance_id)
            and (source is None or str(entry.get("event", "")).startswith(f"{source}."))
        ]
        if limit is not None and len(matches) > limit:
            matches = matches[-limit:]
        return matches


_log_ring_buffer = LogRingBuffer()


def get_log_ring_buffer() -> LogRingBuffer:
    """Return the process-wide log ring buffer (singleton, like structlog's
    own configuration — there is one logging setup per process)."""
    return _log_ring_buffer


def _capture_to_ring_buffer(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    _log_ring_buffer.append(event_dict)
    return event_dict


def _inject_trace_context(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """OTel + structlog correlation (Sprint 16, XEDGE-129): every log entry
    emitted while a span is active gets that span's trace_id/span_id, so a
    trace viewer and the log viewer can be cross-referenced. A no-op outside
    any span (get_current_span() returns an invalid context, same as when
    tracing is disabled entirely — xedge.observability.tracing still installs
    a TracerProvider with no exporters in that case, so this call is always
    safe)."""
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging for JSON output on stdout.

    Idempotent: safe to call multiple times (e.g. on config reload); only the
    first call has effect unless the level differs from the current one.
    """
    global _CONFIGURED

    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {level!r}")

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _inject_trace_context,
            _capture_to_ring_buffer,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """Return a structlog logger bound with a `logger` field.

    Callers should not include sensitive values (passwords, keys, raw tag
    values for sensitive tags) in log fields — see SR-DS-003.
    """
    if not _CONFIGURED:
        configure_logging()
    logger: structlog.typing.FilteringBoundLogger = (
        structlog.get_logger(name) if name else structlog.get_logger()
    )
    return logger
