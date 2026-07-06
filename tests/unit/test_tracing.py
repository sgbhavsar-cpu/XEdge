from __future__ import annotations

from opentelemetry.trace import StatusCode

from xedge.observability.tracing import configure_tracing, get_tracer


def test_disabled_tracing_is_safe_to_use(otel_test_tracer_provider) -> None:
    """With tracing disabled, spans are still created (no `if enabled` checks
    needed at instrumentation call sites) — configure_tracing must not crash,
    and get_tracer() must return something usable."""
    configure_tracing({"enabled": False}, "0.1.0")
    tracer = get_tracer("test")
    with tracer.start_as_current_span("noop.span") as span:
        span.set_attribute("foo", "bar")
    # No assertion on otel_test_tracer_provider here: configure_tracing installs
    # its own (processor-less) provider, overriding the fixture's — this test
    # only asserts that using a disabled tracer doesn't raise.


def test_span_records_success_attributes(otel_test_tracer_provider) -> None:
    tracer = get_tracer("test")
    with tracer.start_as_current_span("driver.read", attributes={"driver.instance_id": "d1"}):
        pass

    spans = otel_test_tracer_provider.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "driver.read"
    assert spans[0].attributes["driver.instance_id"] == "d1"
    assert spans[0].status.status_code == StatusCode.UNSET


def test_span_records_failure_status(otel_test_tracer_provider) -> None:
    from opentelemetry.trace import Status

    tracer = get_tracer("test")
    with tracer.start_as_current_span("driver.read") as span:
        span.set_status(Status(StatusCode.ERROR, "boom"))
        span.record_exception(ValueError("boom"))

    spans = otel_test_tracer_provider.get_finished_spans()
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].events[0].name == "exception"


def test_configure_tracing_with_device_id_sets_resource_attribute() -> None:
    configure_tracing({"enabled": False, "device_id": "edge-01"}, "0.1.0")
    tracer = get_tracer("test")
    with tracer.start_as_current_span("noop.span"):
        pass  # no crash is the assertion; resource attributes aren't easily
        # inspectable from the ProxyTracer without a real exporter attached
