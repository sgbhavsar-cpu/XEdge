from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xedge.core.alarms import AlarmEngine, AlarmRule, AlarmState, build_alarm_rules
from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality


def _tag(value: object, ts: datetime, tag_id: str = "d1/temp") -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=ts,
        value=value,  # type: ignore[arg-type]
        data_type="FLOAT64",
        quality=Quality.GOOD,
        source_driver="d1",
        source_address="0",
    )


def test_tag_with_no_rule_passes_through_unchanged() -> None:
    engine = AlarmEngine({})
    t0 = datetime.now(UTC)
    result = engine.evaluate(_tag(999.0, t0))
    assert result.is_alarm is False
    assert engine.has_rule("d1/temp") is False


def test_high_threshold_trips_alarm() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    t0 = datetime.now(UTC)
    normal = engine.evaluate(_tag(50.0, t0))
    assert normal.is_alarm is False

    tripped = engine.evaluate(_tag(95.0, t0 + timedelta(seconds=1)))
    assert tripped.is_alarm is True
    status = engine.all_status()["d1/temp"]
    assert status.state == AlarmState.ACTIVE
    assert status.condition == "high"
    assert status.active_since is not None


def test_low_threshold_trips_alarm() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", low=10)})
    t0 = datetime.now(UTC)
    tripped = engine.evaluate(_tag(5.0, t0))
    assert tripped.is_alarm is True
    assert engine.all_status()["d1/temp"].condition == "low"


def test_high_high_reported_over_high_when_both_exceeded() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90, high_high=100)})
    t0 = datetime.now(UTC)
    tripped = engine.evaluate(_tag(150.0, t0))
    assert tripped.is_alarm is True
    assert engine.all_status()["d1/temp"].condition == "high_high"


def test_value_returns_to_normal_clears_alarm() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    t0 = datetime.now(UTC)
    engine.evaluate(_tag(95.0, t0))
    assert engine.all_status()["d1/temp"].state == AlarmState.ACTIVE

    cleared = engine.evaluate(_tag(50.0, t0 + timedelta(seconds=1)))
    assert cleared.is_alarm is False
    status = engine.all_status()["d1/temp"]
    assert status.state == AlarmState.NORMAL
    assert status.condition is None
    assert status.active_since is None


def test_rate_of_change_trips_alarm_on_second_sample() -> None:
    engine = AlarmEngine(
        {"d1/temp": AlarmRule(tag_id="d1/temp", rate_of_change_per_second=10)}
    )
    t0 = datetime.now(UTC)
    first = engine.evaluate(_tag(0.0, t0))
    assert first.is_alarm is False  # no prior sample to compare against

    second = engine.evaluate(_tag(50.0, t0 + timedelta(seconds=1)))  # 50/s > 10/s
    assert second.is_alarm is True
    assert engine.all_status()["d1/temp"].condition == "rate_of_change"


def test_rate_of_change_within_limit_does_not_trip() -> None:
    engine = AlarmEngine(
        {"d1/temp": AlarmRule(tag_id="d1/temp", rate_of_change_per_second=100)}
    )
    t0 = datetime.now(UTC)
    engine.evaluate(_tag(0.0, t0))
    result = engine.evaluate(_tag(5.0, t0 + timedelta(seconds=1)))
    assert result.is_alarm is False


def test_acknowledge_active_alarm_transitions_to_active_acked() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    t0 = datetime.now(UTC)
    engine.evaluate(_tag(95.0, t0))

    assert engine.acknowledge("d1/temp", "alice") is True
    status = engine.all_status()["d1/temp"]
    assert status.state == AlarmState.ACTIVE_ACKED
    assert status.acknowledged_by == "alice"
    assert status.acknowledged_at is not None

    # Still in alarm (is_alarm True) while acked but not yet cleared.
    still_alarming = engine.evaluate(_tag(96.0, t0 + timedelta(seconds=1)))
    assert still_alarming.is_alarm is True


def test_acknowledge_returns_false_when_not_active() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    assert engine.acknowledge("d1/temp", "alice") is False  # never evaluated
    assert engine.acknowledge("unknown/tag", "alice") is False


def test_acknowledge_twice_is_a_no_op_the_second_time() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    engine.evaluate(_tag(95.0, datetime.now(UTC)))
    assert engine.acknowledge("d1/temp", "alice") is True
    assert engine.acknowledge("d1/temp", "bob") is False  # already ACTIVE_ACKED, not ACTIVE


def test_shelve_suppresses_is_alarm_but_state_machine_keeps_tracking() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    t0 = datetime.now(UTC)
    engine.evaluate(_tag(95.0, t0))
    engine.shelve("d1/temp", duration_seconds=3600)

    shelved = engine.evaluate(_tag(150.0, t0 + timedelta(seconds=1)))
    assert shelved.is_alarm is False
    status = engine.all_status()["d1/temp"]
    assert status.state == AlarmState.ACTIVE  # still tracked underneath
    assert status.shelved_until is not None


def test_unshelve_restores_is_alarm_reporting() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    t0 = datetime.now(UTC)
    engine.evaluate(_tag(95.0, t0))
    engine.shelve("d1/temp", duration_seconds=3600)
    assert engine.unshelve("d1/temp") is True

    result = engine.evaluate(_tag(96.0, t0 + timedelta(seconds=1)))
    assert result.is_alarm is True


def test_unshelve_returns_false_when_not_shelved() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    assert engine.unshelve("d1/temp") is False


def test_shelve_expires_after_duration() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    t0 = datetime.now(UTC)
    engine.evaluate(_tag(95.0, t0))
    engine.shelve("d1/temp", duration_seconds=5)

    still_shelved = engine.evaluate(_tag(96.0, t0 + timedelta(seconds=2)))
    assert still_shelved.is_alarm is False

    expired = engine.evaluate(_tag(97.0, t0 + timedelta(seconds=10)))
    assert expired.is_alarm is True


def test_non_numeric_value_does_not_crash_threshold_check() -> None:
    engine = AlarmEngine({"d1/status": AlarmRule(tag_id="d1/status", high=90)})
    result = engine.evaluate(_tag("OK", datetime.now(UTC), tag_id="d1/status"))
    assert result.is_alarm is False


def test_build_alarm_rules_from_config() -> None:
    rules = build_alarm_rules(
        [
            {"tag_id": "d1/temp", "high": 90, "low": 10},
            {"tag_id": "d1/flow", "rate_of_change_per_second": 5},
        ]
    )
    assert rules["d1/temp"] == AlarmRule(tag_id="d1/temp", high=90, low=10)
    assert rules["d1/flow"] == AlarmRule(tag_id="d1/flow", rate_of_change_per_second=5)
