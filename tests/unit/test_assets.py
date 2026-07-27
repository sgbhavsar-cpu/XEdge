from __future__ import annotations

from datetime import UTC, datetime

from xedge.core.assets import (
    AssetStorageFilter,
    all_tag_refs,
    asset_backing_instance_ids,
    compute_asset_connection_state,
    compute_suppressed_tag_ids,
    derive_asset_connection_state,
    parse_assets,
    validate_asset_references,
)
from xedge.core.connectivity import ConnectivityState
from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality


def _tag(tag_id: str) -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=1.0,
        data_type="FLOAT64",
        quality=Quality.GOOD,
        source_driver=tag_id.split("/")[0],
        source_address="0",
    )


class TestParseAssets:
    def test_parses_full_metadata_and_parameters(self) -> None:
        assets = parse_assets(
            [
                {
                    "id": "pump-101",
                    "name": "Feedwater Pump 101",
                    "serial_number": "SN-88213",
                    "asset_type": "centrifugal_pump",
                    "make": "Grundfos",
                    "model": "NK 80-250",
                    "firmware_version": "4.2.1",
                    "description": "Primary feedwater pump",
                    "gateway_id": "gw-eastwing",
                    "parameters": [
                        {
                            "tag_ref": "modbus-1/discharge_pressure",
                            "alias": "Discharge Pressure",
                            "unit": "bar",
                            "store": True,
                        },
                        {"tag_ref": "modbus-1/motor_current", "store": False},
                    ],
                }
            ]
        )
        assert len(assets) == 1
        asset = assets[0]
        assert asset.id == "pump-101"
        assert asset.name == "Feedwater Pump 101"
        assert asset.enabled is True
        assert asset.gateway_id == "gw-eastwing"
        assert len(asset.parameters) == 2
        assert asset.parameters[0].alias == "Discharge Pressure"
        assert asset.parameters[0].store is True
        assert asset.parameters[1].store is False
        assert asset.parameters[1].alias is None

    def test_defaults_enabled_true_and_no_parameters(self) -> None:
        assets = parse_assets([{"id": "a1", "name": "Asset One"}])
        assert assets[0].enabled is True
        assert assets[0].parameters == ()


class TestAllTagRefs:
    def test_collects_every_tag_across_drivers_and_groups(self) -> None:
        drivers = [
            {
                "id": "modbus_tcp_01",
                "tag_groups": [
                    {"id": "g1", "tags": [{"id": "temp"}, {"id": "pressure"}]},
                    {"id": "g2", "tags": [{"id": "flow"}]},
                ],
            },
            {"id": "bacnet_01", "tag_groups": [{"id": "g1", "tags": [{"id": "setpoint"}]}]},
        ]
        refs = all_tag_refs(drivers)
        assert refs == {
            "modbus_tcp_01/temp",
            "modbus_tcp_01/pressure",
            "modbus_tcp_01/flow",
            "bacnet_01/setpoint",
        }

    def test_includes_tags_from_a_disabled_driver(self) -> None:
        """Referential integrity is about identifier existence, not
        liveness (ADR-010) -- a disabled driver's tags still count."""
        drivers = [
            {
                "id": "modbus_tcp_01",
                "enabled": False,
                "tag_groups": [{"id": "g1", "tags": [{"id": "temp"}]}],
            }
        ]
        assert all_tag_refs(drivers) == {"modbus_tcp_01/temp"}

    def test_empty_drivers_list_yields_no_refs(self) -> None:
        assert all_tag_refs([]) == set()


class TestValidateAssetReferences:
    _drivers = [
        {"id": "modbus_tcp_01", "tag_groups": [{"id": "g1", "tags": [{"id": "temp"}]}]}
    ]

    def test_no_errors_when_every_tag_ref_resolves(self) -> None:
        config = {
            "drivers": self._drivers,
            "assets": [
                {"id": "a1", "name": "A1", "parameters": [{"tag_ref": "modbus_tcp_01/temp"}]}
            ],
        }
        assert validate_asset_references(config) == []

    def test_no_assets_section_is_valid(self) -> None:
        assert validate_asset_references({"drivers": self._drivers}) == []

    def test_dangling_tag_ref_reported(self) -> None:
        config = {
            "drivers": self._drivers,
            "assets": [
                {
                    "id": "a1",
                    "name": "A1",
                    "parameters": [{"tag_ref": "modbus_tcp_01/nonexistent"}],
                }
            ],
        }
        errors = validate_asset_references(config)
        assert len(errors) == 1
        assert "a1" in errors[0]
        assert "modbus_tcp_01/nonexistent" in errors[0]

    def test_reports_every_dangling_reference_not_just_the_first(self) -> None:
        config = {
            "drivers": self._drivers,
            "assets": [
                {
                    "id": "a1",
                    "name": "A1",
                    "parameters": [{"tag_ref": "no/such1"}, {"tag_ref": "no/such2"}],
                },
                {"id": "a2", "name": "A2", "parameters": [{"tag_ref": "no/such3"}]},
            ],
        }
        assert len(validate_asset_references(config)) == 3


class TestComputeSuppressedTagIds:
    def test_store_false_suppresses(self) -> None:
        assets = parse_assets(
            [{"id": "a1", "name": "A1", "parameters": [{"tag_ref": "d1/t1", "store": False}]}]
        )
        assert compute_suppressed_tag_ids(assets) == {"d1/t1"}

    def test_store_true_is_never_suppressed(self) -> None:
        assets = parse_assets(
            [{"id": "a1", "name": "A1", "parameters": [{"tag_ref": "d1/t1", "store": True}]}]
        )
        assert compute_suppressed_tag_ids(assets) == set()

    def test_a_conflicting_store_true_reference_wins(self) -> None:
        """One asset says don't store this tag, another says do -- the
        project's own documented interpretation (assets.py) is that
        wanting the data wins over not wanting it."""
        assets = parse_assets(
            [
                {"id": "a1", "name": "A1", "parameters": [{"tag_ref": "d1/t1", "store": False}]},
                {"id": "a2", "name": "A2", "parameters": [{"tag_ref": "d1/t1", "store": True}]},
            ]
        )
        assert compute_suppressed_tag_ids(assets) == set()

    def test_no_asset_parameters_suppresses_nothing(self) -> None:
        assert compute_suppressed_tag_ids([]) == set()


class TestAssetStorageFilter:
    def test_suppressed_tag_never_reaches_the_wrapped_callback(self) -> None:
        calls: list[UnifiedTag] = []
        filt = AssetStorageFilter(
            [{"id": "a1", "name": "A1", "parameters": [{"tag_ref": "d1/t1", "store": False}]}]
        )
        wrapped = filt.wrap(lambda stream_key, tag: calls.append(tag))

        wrapped("d1", _tag("d1/t1"))
        wrapped("d1", _tag("d1/t2"))

        assert [t.tag_id for t in calls] == ["d1/t2"]

    def test_refresh_picks_up_a_hot_reloaded_assets_section(self) -> None:
        calls: list[UnifiedTag] = []
        filt = AssetStorageFilter([])
        wrapped = filt.wrap(lambda stream_key, tag: calls.append(tag))

        wrapped("d1", _tag("d1/t1"))
        assert len(calls) == 1

        filt.refresh(
            [{"id": "a1", "name": "A1", "parameters": [{"tag_ref": "d1/t1", "store": False}]}]
        )
        wrapped("d1", _tag("d1/t1"))
        assert len(calls) == 1  # unchanged -- the second call was suppressed


class TestComputeAssetConnectionState:
    def test_all_connected_is_connected(self) -> None:
        result = compute_asset_connection_state(
            [ConnectivityState.CONNECTED, ConnectivityState.CONNECTED]
        )
        assert result is ConnectivityState.CONNECTED

    def test_all_not_connected_is_not_connected(self) -> None:
        result = compute_asset_connection_state(
            [ConnectivityState.NOT_CONNECTED, ConnectivityState.NOT_CONNECTED]
        )
        assert result is ConnectivityState.NOT_CONNECTED

    def test_mixed_connected_and_not_connected_is_degraded(self) -> None:
        result = compute_asset_connection_state(
            [ConnectivityState.CONNECTED, ConnectivityState.NOT_CONNECTED]
        )
        assert result is ConnectivityState.DEGRADED

    def test_all_unknown_is_unknown_not_not_connected(self) -> None:
        """A non-Modbus-only asset shouldn't look like a confirmed
        failure just because its driver types don't report connectivity
        yet (assets.py's documented interpretation)."""
        result = compute_asset_connection_state(
            [ConnectivityState.UNKNOWN, ConnectivityState.UNKNOWN]
        )
        assert result is ConnectivityState.UNKNOWN

    def test_empty_list_is_unknown(self) -> None:
        assert compute_asset_connection_state([]) is ConnectivityState.UNKNOWN

    def test_mixed_connected_and_unknown_is_degraded(self) -> None:
        result = compute_asset_connection_state(
            [ConnectivityState.CONNECTED, ConnectivityState.UNKNOWN]
        )
        assert result is ConnectivityState.DEGRADED


class TestAssetBackingInstanceIds:
    def test_collects_distinct_instance_ids_across_parameters(self) -> None:
        asset = parse_assets(
            [
                {
                    "id": "a1",
                    "name": "A1",
                    "parameters": [
                        {"tag_ref": "modbus_01/temp"},
                        {"tag_ref": "modbus_01/pressure"},
                        {"tag_ref": "bacnet_01/setpoint"},
                    ],
                }
            ]
        )[0]
        assert asset_backing_instance_ids(asset) == {"modbus_01", "bacnet_01"}

    def test_no_parameters_yields_no_instance_ids(self) -> None:
        asset = parse_assets([{"id": "a1", "name": "A1"}])[0]
        assert asset_backing_instance_ids(asset) == set()


class TestDeriveAssetConnectionState:
    def test_looks_up_each_backing_instance_and_combines(self) -> None:
        asset = parse_assets(
            [
                {
                    "id": "a1",
                    "name": "A1",
                    "parameters": [{"tag_ref": "modbus_01/temp"}, {"tag_ref": "bacnet_01/sp"}],
                }
            ]
        )[0]
        result = derive_asset_connection_state(
            asset,
            {"modbus_01": ConnectivityState.CONNECTED, "bacnet_01": ConnectivityState.CONNECTED},
        )
        assert result is ConnectivityState.CONNECTED

    def test_a_missing_instance_is_excluded_not_treated_as_a_state(self) -> None:
        """A disabled driver backing an asset parameter is a valid
        tag_ref (referential integrity doesn't require 'enabled') but
        DriverSupervisor never starts it, so it reports no status at
        all — this must not be silently treated as NOT_CONNECTED."""
        asset = parse_assets(
            [{"id": "a1", "name": "A1", "parameters": [{"tag_ref": "disabled_driver/temp"}]}]
        )[0]
        result = derive_asset_connection_state(asset, {})
        assert result is ConnectivityState.UNKNOWN

    def test_partial_connectivity_across_instances_is_degraded(self) -> None:
        asset = parse_assets(
            [
                {
                    "id": "a1",
                    "name": "A1",
                    "parameters": [{"tag_ref": "modbus_01/temp"}, {"tag_ref": "modbus_02/temp"}],
                }
            ]
        )[0]
        result = derive_asset_connection_state(
            asset,
            {
                "modbus_01": ConnectivityState.CONNECTED,
                "modbus_02": ConnectivityState.NOT_CONNECTED,
            },
        )
        assert result is ConnectivityState.DEGRADED
