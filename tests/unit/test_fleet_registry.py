from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xedge.fleet.registry import DeviceRecord, DeviceRegistry, GatewayConnectionState


def test_register_returns_a_token_that_verifies(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    token = registry.register("dev1", "Line 1 PLC", "0.1.0", heartbeat_interval_seconds=60)

    assert registry.verify_token("dev1", token) is True
    assert registry.verify_token("dev1", "wrong-token") is False
    assert registry.verify_token("unknown-device", token) is False


def test_re_registering_issues_a_new_token_invalidating_the_old_one(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    first_token = registry.register("dev1", None, None, heartbeat_interval_seconds=60)
    second_token = registry.register("dev1", None, None, heartbeat_interval_seconds=60)

    assert first_token != second_token
    assert registry.verify_token("dev1", first_token) is False
    assert registry.verify_token("dev1", second_token) is True


def test_registered_at_is_preserved_across_re_registration(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)
    first_registered_at = registry.get("dev1").registered_at
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)

    assert registry.get("dev1").registered_at == first_registered_at


def test_status_is_unknown_before_any_heartbeat(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)

    assert registry.get("dev1").status == "unknown"


def test_status_is_online_immediately_after_heartbeat(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)
    registry.heartbeat("dev1", "0.1.0", driver_count=2, uptime_seconds=5.0, last_config_apply=None)

    record = registry.get("dev1")
    assert record.status == "online"
    assert record.driver_count == 2
    assert record.uptime_seconds == 5.0


def test_status_is_offline_after_missed_heartbeats() -> None:
    stale_seen_at = datetime.now(UTC) - timedelta(seconds=1000)
    record = DeviceRecord(
        device_id="dev1",
        display_name=None,
        registered_at=datetime.now(UTC),
        agent_version=None,
        heartbeat_interval_seconds=10,
        last_seen_at=stale_seen_at,
        driver_count=None,
        uptime_seconds=None,
        last_config_apply=None,
        has_pending_config=False,
        pending_config_version=0,
        cert_serial_number=None,
        cert_not_after=None,
        serial_number=None,
        make=None,
        protocol=None,
        hardware_firmware_version=None,
    )
    assert record.status == "offline"


def test_heartbeat_preserves_agent_version_and_last_config_apply_when_omitted(
    tmp_path: Path,
) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, "0.1.0", heartbeat_interval_seconds=60)
    registry.heartbeat("dev1", "0.1.0", 1, 1.0, {"version": 1, "success": True, "error": None})
    registry.heartbeat("dev1", None, 2, 2.0, None)

    record = registry.get("dev1")
    assert record.agent_version == "0.1.0"
    assert record.last_config_apply == {"version": 1, "success": True, "error": None}


def test_queue_config_and_take_pending_config_round_trip(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)

    version = registry.queue_config("dev1", {"schema_version": "0.1"})
    assert version == 1
    assert registry.get("dev1").has_pending_config is True

    pending = registry.take_pending_config("dev1")
    assert pending == ({"schema_version": "0.1"}, 1)
    # Cleared after being taken once — not delivered twice.
    assert registry.take_pending_config("dev1") is None
    assert registry.get("dev1").has_pending_config is False


def test_queue_config_for_unknown_device_raises_key_error(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    try:
        registry.queue_config("no-such-device", {})
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_list_devices_is_sorted_by_device_id(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("zzz", None, None, heartbeat_interval_seconds=60)
    registry.register("aaa", None, None, heartbeat_interval_seconds=60)

    assert [d.device_id for d in registry.list_devices()] == ["aaa", "zzz"]


def test_join_token_is_consumable_exactly_once_for_the_right_device(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    token = registry.create_join_token("dev1", ttl_seconds=3600)

    assert registry.consume_join_token("dev1", token) is True
    assert registry.consume_join_token("dev1", token) is False


def test_join_token_is_rejected_for_a_different_device_id(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    token = registry.create_join_token("dev1", ttl_seconds=3600)

    assert registry.consume_join_token("dev2", token) is False
    # Not consumed by the failed attempt above -- the right device can
    # still redeem it.
    assert registry.consume_join_token("dev1", token) is True


def test_join_token_is_rejected_once_expired(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    token = registry.create_join_token("dev1", ttl_seconds=-1)

    assert registry.consume_join_token("dev1", token) is False


def test_unknown_join_token_is_rejected(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")

    assert registry.consume_join_token("dev1", "not-a-real-token") is False


def test_update_metadata_sets_only_the_provided_fields(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", "Original Name", None, heartbeat_interval_seconds=60)

    updated = registry.update_metadata("dev1", {"serial_number": "SN-123", "make": "Acme Gateways"})

    assert updated is True
    record = registry.get("dev1")
    assert record.serial_number == "SN-123"
    assert record.make == "Acme Gateways"
    assert record.display_name == "Original Name"  # untouched -- not in `fields`
    assert record.protocol is None


def test_update_metadata_can_clear_a_field_with_explicit_none(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)
    registry.update_metadata("dev1", {"make": "Acme Gateways"})

    registry.update_metadata("dev1", {"make": None})

    assert registry.get("dev1").make is None


def test_update_metadata_for_unknown_device_returns_false(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")

    assert registry.update_metadata("no-such-device", {"make": "Acme"}) is False


def test_update_metadata_rejects_an_unknown_field(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)

    with pytest.raises(ValueError, match="not_a_real_field"):
        registry.update_metadata("dev1", {"not_a_real_field": "x"})


def test_connection_state_is_inactive_before_any_heartbeat(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)

    assert registry.get("dev1").connection_state == GatewayConnectionState.INACTIVE


def test_connection_state_is_active_immediately_after_heartbeat(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)
    registry.heartbeat("dev1", None, None, None, None)

    assert registry.get("dev1").connection_state == GatewayConnectionState.ACTIVE


def test_connection_state_is_connected_when_late_but_within_the_offline_threshold() -> None:
    record = DeviceRecord(
        device_id="dev1",
        display_name=None,
        registered_at=datetime.now(UTC),
        agent_version=None,
        heartbeat_interval_seconds=10,
        last_seen_at=datetime.now(UTC) - timedelta(seconds=15),
        driver_count=None,
        uptime_seconds=None,
        last_config_apply=None,
        has_pending_config=False,
        pending_config_version=0,
        cert_serial_number=None,
        cert_not_after=None,
        serial_number=None,
        make=None,
        protocol=None,
        hardware_firmware_version=None,
    )
    assert record.connection_state == GatewayConnectionState.CONNECTED
    assert record.status == "online"  # status's own threshold (3x) isn't reached yet either


def test_connection_state_is_disconnected_past_the_offline_threshold() -> None:
    record = DeviceRecord(
        device_id="dev1",
        display_name=None,
        registered_at=datetime.now(UTC),
        agent_version=None,
        heartbeat_interval_seconds=10,
        last_seen_at=datetime.now(UTC) - timedelta(seconds=1000),
        driver_count=None,
        uptime_seconds=None,
        last_config_apply=None,
        has_pending_config=False,
        pending_config_version=0,
        cert_serial_number=None,
        cert_not_after=None,
        serial_number=None,
        make=None,
        protocol=None,
        hardware_firmware_version=None,
    )
    assert record.connection_state == GatewayConnectionState.DISCONNECTED


def test_record_certificate_issued_updates_the_device_record(tmp_path: Path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.db")
    registry.register("dev1", None, None, heartbeat_interval_seconds=60)
    not_after = datetime.now(UTC) + timedelta(days=90)

    registry.record_certificate_issued("dev1", 12345, not_after)

    record = registry.get("dev1")
    assert record.cert_serial_number == "12345"
    assert record.cert_not_after == not_after
