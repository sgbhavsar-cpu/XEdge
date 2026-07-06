"""OpenTelemetry metrics + Prometheus exposition (Sprint 16, XEDGE-128/130).

Every instrument here is *observable* (callback-based): OTel invokes the
callback at scrape time, reading straight from the metrics structures that
already exist — `DriverMetrics` (`xedge.drivers.base`), `ConnectorMetrics`
(`xedge.northbound.base`, whose own docstring says "mirrors DriverMetrics"),
and `RingBufferMetrics` (`xedge.store.ring_buffer`). No new increment call
sites anywhere in the codebase; this module only reads state that already
exists for other reasons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from prometheus_client import REGISTRY, CollectorRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable

    from xedge.core.supervisor import DriverSupervisor
    from xedge.northbound.dispatcher import NorthboundDispatcher
    from xedge.store.ring_buffer import RingBufferManager


def configure_metrics(
    supervisor: DriverSupervisor,
    dispatcher: NorthboundDispatcher | None,
    ring_buffers: RingBufferManager,
    service_version: str,
    registry: CollectorRegistry = REGISTRY,
) -> None:
    """Registers observable instruments against a Prometheus registry (via
    `PrometheusMetricReader`) — `GET /metrics` (`xedge.api.server`) then
    just calls `prometheus_client.generate_latest()` against that same
    default registry. `registry` defaults to the global default (production
    use, one call per process); tests pass an isolated `CollectorRegistry()`
    so repeated calls across a test session don't accumulate stale
    collectors in shared global state."""
    resource = Resource.create({"service.name": "xedge", "service.version": service_version})
    provider = MeterProvider(
        resource=resource, metric_readers=[PrometheusMetricReader(registry=registry)]
    )
    meter = provider.get_meter("xedge")

    def _driver_reads(_options: CallbackOptions) -> Iterable[Observation]:
        for status_ in supervisor.all_status().values():
            yield Observation(
                status_.metrics.tag_read_count, {"instance_id": status_.instance_id}
            )

    def _driver_errors(_options: CallbackOptions) -> Iterable[Observation]:
        for status_ in supervisor.all_status().values():
            yield Observation(status_.metrics.error_count, {"instance_id": status_.instance_id})

    def _driver_reconnects(_options: CallbackOptions) -> Iterable[Observation]:
        for status_ in supervisor.all_status().values():
            yield Observation(
                status_.metrics.reconnect_count, {"instance_id": status_.instance_id}
            )

    meter.create_observable_counter(
        "xedge_driver_tag_reads_total",
        callbacks=[_driver_reads],
        description="Total tag reads per driver instance",
    )
    meter.create_observable_counter(
        "xedge_driver_errors_total",
        callbacks=[_driver_errors],
        description="Total read errors per driver instance",
    )
    meter.create_observable_counter(
        "xedge_driver_reconnects_total",
        callbacks=[_driver_reconnects],
        description="Total reconnect attempts per driver instance",
    )

    if dispatcher is not None:

        def _northbound_published(_options: CallbackOptions) -> Iterable[Observation]:
            yield Observation(dispatcher.get_metrics().published_count)

        def _northbound_errors(_options: CallbackOptions) -> Iterable[Observation]:
            yield Observation(dispatcher.get_metrics().error_count)

        meter.create_observable_counter(
            "xedge_northbound_published_total",
            callbacks=[_northbound_published],
            description="Total tags published northbound",
        )
        meter.create_observable_counter(
            "xedge_northbound_errors_total",
            callbacks=[_northbound_errors],
            description="Total northbound publish errors",
        )

    def _ring_buffer_depth(_options: CallbackOptions) -> Iterable[Observation]:
        for stream_key in ring_buffers.stream_keys():
            stream_metrics = ring_buffers.metrics(stream_key)
            if stream_metrics is not None:
                yield Observation(stream_metrics.depth, {"stream_key": stream_key})

    def _ring_buffer_evicted(_options: CallbackOptions) -> Iterable[Observation]:
        for stream_key in ring_buffers.stream_keys():
            stream_metrics = ring_buffers.metrics(stream_key)
            if stream_metrics is not None:
                yield Observation(stream_metrics.evicted_count, {"stream_key": stream_key})

    meter.create_observable_gauge(
        "xedge_ring_buffer_depth",
        callbacks=[_ring_buffer_depth],
        description="Current sample count per ring buffer stream",
    )
    meter.create_observable_counter(
        "xedge_ring_buffer_evicted_total",
        callbacks=[_ring_buffer_evicted],
        description="Total samples evicted per ring buffer stream",
    )
