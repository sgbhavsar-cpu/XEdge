from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xedge.fleet.registry import (
    DeviceRecord,
    DeviceRegistry,
    DeviceTenantConflictError,
    GatewayConnectionState,
)


async def test_register_returns_a_token_that_verifies(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    token = await fleet_registry.register(
        fleet_default_tenant_id, "dev1", "Line 1 PLC", "0.1.0", heartbeat_interval_seconds=60
    )

    assert await fleet_registry.verify_token("dev1", token) is True
    assert await fleet_registry.verify_token("dev1", "wrong-token") is False
    assert await fleet_registry.verify_token("unknown-device", token) is False


async def test_re_registering_issues_a_new_token_invalidating_the_old_one(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    first_token = await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )
    second_token = await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    assert first_token != second_token
    assert await fleet_registry.verify_token("dev1", first_token) is False
    assert await fleet_registry.verify_token("dev1", second_token) is True


async def test_registered_at_is_preserved_across_re_registration(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )
    first_registered_at = (await fleet_registry.get(fleet_default_tenant_id, "dev1")).registered_at
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.registered_at == first_registered_at


async def test_register_rejects_a_device_id_already_owned_by_another_tenant(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str, other_tenant_id: str
) -> None:
    """XEDGE-504: "a device cannot enroll across tenants" — device_id is
    globally unique (xedge.fleet.db_models module docstring), so a second
    tenant reusing it must be rejected, not silently move ownership."""
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    with pytest.raises(DeviceTenantConflictError):
        await fleet_registry.register(
            other_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
        )


async def test_status_is_unknown_before_any_heartbeat(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.status == "unknown"


async def test_status_is_online_immediately_after_heartbeat(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )
    await fleet_registry.heartbeat(
        "dev1", "0.1.0", driver_count=2, uptime_seconds=5.0, last_config_apply=None
    )

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
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


async def test_heartbeat_preserves_agent_version_and_last_config_apply_when_omitted(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, "0.1.0", heartbeat_interval_seconds=60
    )
    await fleet_registry.heartbeat(
        "dev1", "0.1.0", 1, 1.0, {"version": 1, "success": True, "error": None}
    )
    await fleet_registry.heartbeat("dev1", None, 2, 2.0, None)

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.agent_version == "0.1.0"
    assert record.last_config_apply == {"version": 1, "success": True, "error": None}


async def test_queue_config_and_take_pending_config_round_trip(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    version = await fleet_registry.queue_config(
        fleet_default_tenant_id, "dev1", {"schema_version": "0.1"}
    )
    assert version == 1
    assert (await fleet_registry.get(fleet_default_tenant_id, "dev1")).has_pending_config is True

    pending = await fleet_registry.take_pending_config("dev1")
    assert pending == ({"schema_version": "0.1"}, 1)
    # Cleared after being taken once — not delivered twice.
    assert await fleet_registry.take_pending_config("dev1") is None
    assert (await fleet_registry.get(fleet_default_tenant_id, "dev1")).has_pending_config is False


async def test_queue_config_for_unknown_device_raises_key_error(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    with pytest.raises(KeyError):
        await fleet_registry.queue_config(fleet_default_tenant_id, "no-such-device", {})


async def test_queue_config_for_a_device_in_another_tenant_raises_key_error(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str, other_tenant_id: str
) -> None:
    """XEDGE-503: an operator in tenant B must not be able to affect a
    device that belongs to tenant A, even by device_id."""
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    with pytest.raises(KeyError):
        await fleet_registry.queue_config(other_tenant_id, "dev1", {})


async def test_list_devices_is_sorted_by_device_id(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "zzz", None, None, heartbeat_interval_seconds=60
    )
    await fleet_registry.register(
        fleet_default_tenant_id, "aaa", None, None, heartbeat_interval_seconds=60
    )

    devices = await fleet_registry.list_devices(fleet_default_tenant_id)
    assert [d.device_id for d in devices] == ["aaa", "zzz"]


async def test_join_token_is_consumable_exactly_once_for_the_right_device(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    token = await fleet_registry.create_join_token(
        fleet_default_tenant_id, "dev1", ttl_seconds=3600
    )

    assert await fleet_registry.consume_join_token("dev1", token) == fleet_default_tenant_id
    assert await fleet_registry.consume_join_token("dev1", token) is None


async def test_join_token_is_rejected_for_a_different_device_id(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    token = await fleet_registry.create_join_token(
        fleet_default_tenant_id, "dev1", ttl_seconds=3600
    )

    assert await fleet_registry.consume_join_token("dev2", token) is None
    # Not consumed by the failed attempt above -- the right device can
    # still redeem it.
    assert await fleet_registry.consume_join_token("dev1", token) == fleet_default_tenant_id


async def test_join_token_is_rejected_once_expired(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    token = await fleet_registry.create_join_token(fleet_default_tenant_id, "dev1", ttl_seconds=-1)

    assert await fleet_registry.consume_join_token("dev1", token) is None


async def test_unknown_join_token_is_rejected(fleet_registry: DeviceRegistry) -> None:
    assert await fleet_registry.consume_join_token("dev1", "not-a-real-token") is None


async def test_create_join_token_rejects_a_device_id_already_owned_by_another_tenant(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str, other_tenant_id: str
) -> None:
    """XEDGE-504: fails fast at provisioning time rather than waiting for
    a doomed-to-fail enrollment attempt."""
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    with pytest.raises(DeviceTenantConflictError):
        await fleet_registry.create_join_token(other_tenant_id, "dev1", ttl_seconds=3600)


async def test_update_metadata_sets_only_the_provided_fields(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", "Original Name", None, heartbeat_interval_seconds=60
    )

    updated = await fleet_registry.update_metadata(
        fleet_default_tenant_id, "dev1", {"serial_number": "SN-123", "make": "Acme Gateways"}
    )

    assert updated is True
    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.serial_number == "SN-123"
    assert record.make == "Acme Gateways"
    assert record.display_name == "Original Name"  # untouched -- not in `fields`
    assert record.protocol is None


async def test_update_metadata_can_clear_a_field_with_explicit_none(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )
    await fleet_registry.update_metadata(fleet_default_tenant_id, "dev1", {"make": "Acme Gateways"})

    await fleet_registry.update_metadata(fleet_default_tenant_id, "dev1", {"make": None})

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.make is None


async def test_update_metadata_for_unknown_device_returns_false(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    updated = await fleet_registry.update_metadata(
        fleet_default_tenant_id, "no-such-device", {"make": "Acme"}
    )
    assert updated is False


async def test_update_metadata_for_a_device_in_another_tenant_returns_false(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str, other_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    updated = await fleet_registry.update_metadata(other_tenant_id, "dev1", {"make": "Evil Corp"})

    assert updated is False
    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.make is None  # unaffected by the other tenant's attempt


async def test_update_metadata_rejects_an_unknown_field(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    with pytest.raises(ValueError, match="not_a_real_field"):
        await fleet_registry.update_metadata(
            fleet_default_tenant_id, "dev1", {"not_a_real_field": "x"}
        )


async def test_connection_state_is_inactive_before_any_heartbeat(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.connection_state == GatewayConnectionState.INACTIVE


async def test_connection_state_is_active_immediately_after_heartbeat(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )
    await fleet_registry.heartbeat("dev1", None, None, None, None)

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.connection_state == GatewayConnectionState.ACTIVE


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


async def test_record_certificate_issued_updates_the_device_record(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )
    not_after = datetime.now(UTC) + timedelta(days=90)

    await fleet_registry.record_certificate_issued("dev1", 12345, not_after)

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.cert_serial_number == "12345"
    assert record.cert_not_after == not_after


async def test_get_and_list_devices_do_not_see_another_tenants_device(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str, other_tenant_id: str
) -> None:
    """XEDGE-503's own stated requirement: "every query path needs a test
    proving it cannot leak across tenants." `get`/`list_devices` are the
    two read paths an operator's dashboard/CLI ultimately goes through."""
    await fleet_registry.register(
        fleet_default_tenant_id, "dev1", "Tenant A's device", None, heartbeat_interval_seconds=60
    )

    assert await fleet_registry.get(other_tenant_id, "dev1") is None
    assert await fleet_registry.list_devices(other_tenant_id) == []
    assert (await fleet_registry.get(fleet_default_tenant_id, "dev1")) is not None
    assert len(await fleet_registry.list_devices(fleet_default_tenant_id)) == 1


async def test_verify_token_and_heartbeat_are_not_tenant_scoped(
    fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    """Device-facing methods stay keyed on device_id alone (no tenant_id
    parameter at all) — a device proves itself via device_token + mTLS,
    never by asserting a tenant (xedge.fleet.registry module docstring)."""
    token = await fleet_registry.register(
        fleet_default_tenant_id, "dev1", None, None, heartbeat_interval_seconds=60
    )

    assert await fleet_registry.verify_token("dev1", token) is True
    await fleet_registry.heartbeat("dev1", "0.1.0", 1, 1.0, None)
    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.status == "online"
