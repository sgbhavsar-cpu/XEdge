"""OpenTelemetry tracing (Sprint 16, XEDGE-126/127) — spans for driver
reads, pipeline processing, store writes, and northbound publishes.

Deliberately exports over OTLP/HTTP, not the sprint story's literal
"OTLP/gRPC": `opentelemetry-exporter-otlp-proto-grpc` pulls in `grpcio`, a
heavy C-extension dependency with real cross-compilation cost, working
against ADR-007's repeatedly-stated 1GB-RAM ARM target. The HTTP exporter
carries the identical OTLP protocol over plain `requests` (already a
transitive dependency) for a much lighter footprint.

Tracing defaults to *disabled* (unlike `tls`/`system_tags`, which default
on) — a `TracerProvider` with nowhere to send spans is pure overhead, and
the dev collector stack (Grafana/Tempo/Loki, XEDGE-131) is out of scope for
this pass. When enabled with no `otlp_endpoint` configured, spans print to
the console instead (useful for local verification without standing up a
real collector) rather than silently going nowhere.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Tracer


def configure_tracing(config: dict[str, Any], service_version: str) -> None:
    """Idempotent-in-intent: call once at startup. If tracing is disabled,
    installs a bare `TracerProvider` with no span processors — spans are
    still created (so instrumented code paths never need an `if enabled`
    check of their own) but are simply dropped, at negligible cost."""
    resource_attributes: dict[str, Any] = {
        "service.name": "xedge",
        "service.version": service_version,
    }
    if config.get("device_id"):
        resource_attributes["device.id"] = config["device_id"]
    provider = TracerProvider(resource=Resource.create(resource_attributes))

    if config.get("enabled", False):
        otlp_endpoint = config.get("otlp_endpoint")
        if otlp_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
            )
        else:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)


def get_tracer(name: str) -> Tracer:
    return trace.get_tracer(name)
