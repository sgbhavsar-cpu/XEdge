from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import opentelemetry.trace as _otel_trace_module
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.fixtures.bacnet_device import bacnet_test_device  # noqa: F401 — re-exported fixture
from tests.fixtures.mqtt_broker import (  # noqa: F401 — re-exported fixtures
    mqtt_broker,
    mqtt_broker_mtls,
    mqtt_broker_tls,
)
from tests.fixtures.opcua_server import opcua_test_server  # noqa: F401 — re-exported fixture
from tests.fixtures.smtp_server import (  # noqa: F401 — re-exported fixtures
    smtp_server,
    smtp_server_requiring_auth,
    smtp_server_starttls,
)
from tests.fixtures.snmp_agent import snmp_test_agent  # noqa: F401 — re-exported fixture
from xedge.drivers.base import TagUpdate

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_SCHEMA_PATH = REPO_ROOT / "config" / "schema" / "xedge-core.schema.json"


@pytest.fixture
def core_schema_path() -> Path:
    return CORE_SCHEMA_PATH


@pytest.fixture
def tag_queue() -> Iterator[asyncio.Queue[TagUpdate]]:
    yield asyncio.Queue(maxsize=1000)


_otel_session_provider: TracerProvider | None = None


@pytest.fixture(scope="session", autouse=True)
def _otel_session_span_exporter() -> InMemorySpanExporter:
    """Installs exactly one in-memory-backed TracerProvider for the whole
    test session, before the first test body runs (`autouse=True` at session
    scope guarantees this).

    A *per-test* provider swap was tried first and does not work: OTel's
    `get_tracer()` (what every module here calls once, at import time, via
    `xedge.observability.tracing.get_tracer`) returns a `ProxyTracer` when no
    real provider is installed yet — and a `ProxyTracer` resolves its
    delegate *once*, permanently, the first time any span is created against
    it, then ignores every later `set_tracer_provider` call (confirmed
    directly against the SDK: swapping `_TRACER_PROVIDER` between two calls
    against the same already-resolved `ProxyTracer` had zero effect on the
    second call). Since every production module's tracer is a long-lived
    module-level object reused across the whole test session, only the
    *first* test to exercise it would ever see its spans under a per-test
    provider. Installing one provider for the session and clearing the
    exporter's collected spans between tests (see `otel_test_tracer_provider`
    below) sidesteps the caching entirely.
    """
    global _otel_session_provider
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _otel_session_provider = provider
    _otel_trace_module._TRACER_PROVIDER = provider  # noqa: SLF001 — see docstring
    return exporter


@pytest.fixture
def otel_test_tracer_provider(
    _otel_session_span_exporter: InMemorySpanExporter,
) -> Iterator[InMemorySpanExporter]:
    """Per-test view onto the session-wide exporter: cleared before and
    after so each test only sees spans it created itself."""
    _otel_session_span_exporter.clear()
    yield _otel_session_span_exporter
    _otel_session_span_exporter.clear()


@pytest.fixture(autouse=True)
def _restore_otel_session_provider(
    _otel_session_span_exporter: InMemorySpanExporter,
) -> Iterator[None]:
    """A test that calls the real `xedge.observability.tracing.configure_tracing`
    (not just this fixture module's direct-assignment bypass) fires OTel's
    process-wide "TracerProvider already set" guard for good — no later call
    can install a different provider through the guarded path again. Restore
    `_TRACER_PROVIDER` back to the session's shared provider after every
    test via the same direct-assignment bypass, so one test calling
    `configure_tracing` doesn't silently blind every later test's
    `otel_test_tracer_provider` fixture for the rest of the run."""
    yield
    _otel_trace_module._TRACER_PROVIDER = _otel_session_provider  # noqa: SLF001
