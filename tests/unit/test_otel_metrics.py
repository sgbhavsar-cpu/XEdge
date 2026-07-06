from __future__ import annotations

from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, generate_latest

from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverInstanceStatus, DriverState
from xedge.drivers.base import DriverMetrics, Quality
from xedge.northbound.base import ConnectorMetrics
from xedge.observability.otel_metrics import configure_metrics
from xedge.store.ring_buffer import RingBufferManager


class _FakeSupervisor:
    def __init__(self, statuses: dict[str, DriverInstanceStatus]) -> None:
        self._statuses = statuses

    def all_status(self) -> dict[str, DriverInstanceStatus]:
        return self._statuses


class _FakeDispatcher:
    def __init__(self, metrics: ConnectorMetrics) -> None:
        self._metrics = metrics

    def get_metrics(self) -> ConnectorMetrics:
        return self._metrics


def _scrape(registry: CollectorRegistry) -> str:
    return generate_latest(registry).decode("utf-8")


def test_driver_metrics_are_exported_per_instance() -> None:
    supervisor = _FakeSupervisor(
        {
            "modbus_sim_01": DriverInstanceStatus(
                instance_id="modbus_sim_01",
                driver_type="modbus_tcp",
                state=DriverState.RUNNING,
                consecutive_failures=0,
                last_error=None,
                metrics=DriverMetrics(tag_read_count=42, error_count=3, reconnect_count=1),
            )
        }
    )
    registry = CollectorRegistry()
    configure_metrics(supervisor, None, RingBufferManager(), "0.1.0", registry=registry)

    output = _scrape(registry)
    assert 'xedge_driver_tag_reads_total{instance_id="modbus_sim_01"' in output
    assert "} 42.0" in output
    assert 'xedge_driver_errors_total{instance_id="modbus_sim_01"' in output
    assert 'xedge_driver_reconnects_total{instance_id="modbus_sim_01"' in output


def test_northbound_metrics_are_exported_when_dispatcher_present() -> None:
    dispatcher = _FakeDispatcher(
        ConnectorMetrics(published_count=999, error_count=7, reconnect_count=2)
    )
    registry = CollectorRegistry()
    configure_metrics(_FakeSupervisor({}), dispatcher, RingBufferManager(), "0.1.0", registry=registry)

    output = _scrape(registry)
    assert "xedge_northbound_published_total" in output
    assert "xedge_northbound_errors_total" in output


def test_northbound_metrics_absent_when_no_dispatcher() -> None:
    registry = CollectorRegistry()
    configure_metrics(_FakeSupervisor({}), None, RingBufferManager(), "0.1.0", registry=registry)
    output = _scrape(registry)
    assert "xedge_northbound_published_total" not in output


def test_ring_buffer_metrics_are_exported_per_stream() -> None:
    ring_buffers = RingBufferManager()
    ring_buffers.push(
        "modbus_sim_01",
        UnifiedTag(
            tag_id="t1",
            timestamp=datetime.now(UTC),
            value=1,
            data_type="float",
            quality=Quality.GOOD,
            source_driver="modbus_sim_01",
            source_address="1",
        ),
    )
    registry = CollectorRegistry()
    configure_metrics(_FakeSupervisor({}), None, ring_buffers, "0.1.0", registry=registry)

    output = _scrape(registry)
    assert 'xedge_ring_buffer_depth{' in output
    assert 'stream_key="modbus_sim_01"' in output
    assert "xedge_ring_buffer_evicted_total" in output
