from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xedge.core.supervisor import DriverInstanceStatus, DriverState
from xedge.core.system_tags import (
    SYSTEM_TAG_NAMES,
    _ReadRateTracker,
    build_system_tags,
    system_tag_id,
)
from xedge.drivers.base import DriverMetrics


def test_system_tag_id_uses_reserved_sub_namespace() -> None:
    assert system_tag_id("modbus_01", "status") == "modbus_01/_system/status"


def test_rate_tracker_computes_rate_from_two_samples() -> None:
    tracker = _ReadRateTracker()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    tracker.sample("d1", 0, t0)
    rates = tracker.sample("d1", 10, t0 + timedelta(seconds=1))

    assert rates["reads_per_second"] == 10.0


def test_rate_tracker_first_sample_reports_zero() -> None:
    tracker = _ReadRateTracker()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    rates = tracker.sample("d1", 42, now)

    assert rates == {"reads_per_second": 0.0, "reads_per_minute": 0.0, "reads_per_hour": 0.0}


def test_rate_tracker_is_independent_per_instance() -> None:
    tracker = _ReadRateTracker()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    tracker.sample("d1", 0, t0)
    tracker.sample("d2", 0, t0)
    rates_d1 = tracker.sample("d1", 100, t0 + timedelta(seconds=1))
    rates_d2 = tracker.sample("d2", 5, t0 + timedelta(seconds=1))

    assert rates_d1["reads_per_second"] == 100.0
    assert rates_d2["reads_per_second"] == 5.0


def test_rate_tracker_averages_over_longer_window_using_oldest_available_sample() -> None:
    # Driver has only ~30s of history — "per hour" falls back to averaging
    # over whatever history actually exists rather than a full 3600s window.
    tracker = _ReadRateTracker()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    tracker.sample("d1", 0, t0)
    rates = tracker.sample("d1", 60, t0 + timedelta(seconds=30))

    assert (
        rates["reads_per_hour"] == 2.0
    )  # 60 reads / 30s = 2/s, reported as the only rate available


def _status(**overrides: object) -> DriverInstanceStatus:
    defaults: dict[str, object] = {
        "instance_id": "modbus_01",
        "driver_type": "modbus_tcp",
        "state": DriverState.RUNNING,
        "consecutive_failures": 0,
        "last_error": None,
        "metrics": DriverMetrics(tag_read_count=100, error_count=2),
        "tag_count": 5,
        "state_changed_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return DriverInstanceStatus(**defaults)  # type: ignore[arg-type]


def test_build_system_tags_produces_all_nine_names_with_reserved_ids() -> None:
    status = _status()
    now = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    rates = {"reads_per_second": 1.5, "reads_per_minute": 1.2, "reads_per_hour": 1.0}

    tags = build_system_tags(status, rates, now)

    assert {tag.tag_id for tag in tags} == {
        system_tag_id("modbus_01", name) for name in SYSTEM_TAG_NAMES
    }
    by_name = {tag.tag_id.rsplit("/", 1)[-1]: tag for tag in tags}
    assert by_name["status"].value == "running"
    assert by_name["tag_count"].value == 5
    assert by_name["error_count"].value == 2
    assert by_name["reads_per_second"].value == 1.5


def test_build_system_tags_uptime_is_zero_when_not_running() -> None:
    status = _status(state=DriverState.BACKOFF)
    now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
    rates = {"reads_per_second": 0.0, "reads_per_minute": 0.0, "reads_per_hour": 0.0}

    tags = build_system_tags(status, rates, now)

    by_name = {tag.tag_id.rsplit("/", 1)[-1]: tag for tag in tags}
    assert by_name["uptime_seconds"].value == 0.0


def test_build_system_tags_uptime_counts_from_state_changed_at_when_running() -> None:
    status = _status(state=DriverState.RUNNING, state_changed_at=datetime(2026, 1, 1, tzinfo=UTC))
    now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
    rates = {"reads_per_second": 0.0, "reads_per_minute": 0.0, "reads_per_hour": 0.0}

    tags = build_system_tags(status, rates, now)

    by_name = {tag.tag_id.rsplit("/", 1)[-1]: tag for tag in tags}
    assert by_name["uptime_seconds"].value == 60.0
