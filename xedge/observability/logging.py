"""Structured JSON logging setup (structlog), per FR-DF-006 / NFR-M / SR-DS-003.

Renders one JSON object per line to stdout, suitable for capture by the
Docker/systemd journal and forwarding to a SIEM via Fluentd or syslog-ng.
"""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


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
