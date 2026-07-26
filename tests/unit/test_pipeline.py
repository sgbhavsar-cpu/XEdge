from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from xedge.core.pipeline import (
    DeadbandConfig,
    DeadbandFilter,
    ScalingConfig,
    TagPipelineConfig,
    UnifiedTag,
    normalize,
)
from xedge.drivers.base import Quality, TagUpdate


@pytest.mark.parametrize(
    ("value", "expected_data_type"),
    [
        (True, "BOOL"),
        (42, "INT64"),
        (3.14, "FLOAT64"),
        ("hello", "STRING"),
        (b"\x01\x02", "BYTES"),
    ],
)
def test_normalize_maps_data_type(value: object, expected_data_type: str) -> None:
    update = TagUpdate(
        tag_id="modbus_tcp_01/temperature_01",
        timestamp=datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        quality=Quality.GOOD,
        source_driver="modbus_tcp_01",
        source_address="40001",
    )
    tag = normalize(update)
    assert tag.data_type == expected_data_type
    assert tag.tag_id == update.tag_id
    assert tag.value == value
    assert tag.quality == Quality.GOOD
    assert tag.source_driver == "modbus_tcp_01"
    assert tag.source_address == "40001"
    assert tag.is_alarm is False


def test_normalize_preserves_metadata_without_aliasing() -> None:
    update = TagUpdate(
        tag_id="t1",
        timestamp=datetime.now(UTC),
        value=1,
        quality=Quality.GOOD,
        source_driver="d1",
        source_address="0",
        metadata={"modbus_exception": None, "request_latency_ms": 12},
    )
    tag = normalize(update)
    assert tag.metadata["modbus_exception"] is None
    assert tag.metadata["request_latency_ms"] == 12
    tag.metadata["mutated"] = True
    assert "mutated" not in update.metadata


def test_normalize_bad_quality_passthrough() -> None:
    update = TagUpdate(
        tag_id="t1",
        timestamp=datetime.now(UTC),
        value=0,
        quality=Quality.BAD,
        source_driver="d1",
        source_address="1",
        metadata={"modbus_exception": 2},
    )
    tag = normalize(update)
    assert tag.quality == Quality.BAD
    assert tag.metadata["modbus_exception"] == 2


class TestTimestampResolution:
    def test_no_source_timestamp_falls_back_to_ingestion_and_flags_estimated(self) -> None:
        ingestion_ts = datetime.now(UTC)
        update = TagUpdate(
            tag_id="t1",
            timestamp=ingestion_ts,
            value=1,
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="0",
        )
        tag = normalize(update)
        assert tag.timestamp == ingestion_ts
        assert tag.metadata["timestamp_estimated"] is True

    def test_source_timestamp_used_when_present_and_not_flagged(self) -> None:
        ingestion_ts = datetime.now(UTC)
        source_ts = ingestion_ts - timedelta(seconds=5)
        update = TagUpdate(
            tag_id="t1",
            timestamp=ingestion_ts,
            source_timestamp=source_ts,
            value=1,
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="0",
        )
        tag = normalize(update)
        assert tag.timestamp == source_ts
        assert "timestamp_estimated" not in tag.metadata


class TestEngineeringUnitScaling:
    def test_scaling_applies_scale_and_offset(self) -> None:
        update = TagUpdate(
            tag_id="t1",
            timestamp=datetime.now(UTC),
            value=100,
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="0",
        )
        config = TagPipelineConfig(
            scaling=ScalingConfig(scale=0.1, offset=-10.0), engineering_unit="°C"
        )
        tag = normalize(update, config)
        assert tag.value == pytest.approx(0.0)  # 100 * 0.1 + -10.0
        assert tag.data_type == "FLOAT64"
        assert tag.engineering_unit == "°C"

    def test_scaling_does_not_apply_to_bool(self) -> None:
        update = TagUpdate(
            tag_id="t1",
            timestamp=datetime.now(UTC),
            value=True,
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="0",
        )
        config = TagPipelineConfig(scaling=ScalingConfig(scale=2.0, offset=1.0))
        tag = normalize(update, config)
        assert tag.value is True
        assert tag.data_type == "BOOL"

    def test_no_config_leaves_value_unscaled(self) -> None:
        update = TagUpdate(
            tag_id="t1",
            timestamp=datetime.now(UTC),
            value=42,
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="0",
        )
        tag = normalize(update)
        assert tag.value == 42
        assert tag.data_type == "INT64"


class TestDeadbandFilter:
    def _tag(
        self,
        value: object,
        quality: Quality = Quality.GOOD,
        tag_id: str = "t1",
        is_alarm: bool = False,
    ) -> UnifiedTag:
        return UnifiedTag(
            tag_id=tag_id,
            timestamp=datetime.now(UTC),
            value=value,  # type: ignore[arg-type]
            data_type="FLOAT64",
            quality=quality,
            source_driver="d1",
            source_address="0",
            is_alarm=is_alarm,
        )

    def test_first_value_always_published(self) -> None:
        deadband_filter = DeadbandFilter()
        assert (
            deadband_filter.should_publish(self._tag(10.0), DeadbandConfig("absolute", 5.0)) is True
        )

    def test_quality_change_always_published_even_within_deadband(self) -> None:
        deadband_filter = DeadbandFilter()
        deadband_filter.should_publish(self._tag(10.0), DeadbandConfig("absolute", 5.0))
        assert (
            deadband_filter.should_publish(
                self._tag(10.0, quality=Quality.BAD), DeadbandConfig("absolute", 5.0)
            )
            is True
        )

    def test_alarm_transition_always_published_even_within_deadband(self) -> None:
        deadband_filter = DeadbandFilter()
        deadband = DeadbandConfig("absolute", 5.0)
        deadband_filter.should_publish(self._tag(100.0, is_alarm=False), deadband)
        # Value change (2.0) is within the deadband, but is_alarm flipped
        # True -> must publish anyway (Sprint 31, XEDGE-224).
        assert deadband_filter.should_publish(self._tag(102.0, is_alarm=True), deadband) is True
        # Same is_alarm as the last published sample and within deadband -> suppressed.
        assert deadband_filter.should_publish(self._tag(103.0, is_alarm=True), deadband) is False
        # Clearing the alarm is itself a transition -> must publish.
        assert deadband_filter.should_publish(self._tag(103.5, is_alarm=False), deadband) is True

    def test_absolute_deadband_suppresses_small_changes(self) -> None:
        deadband_filter = DeadbandFilter()
        deadband = DeadbandConfig("absolute", 5.0)
        deadband_filter.should_publish(self._tag(100.0), deadband)
        assert deadband_filter.should_publish(self._tag(102.0), deadband) is False
        assert deadband_filter.should_publish(self._tag(106.0), deadband) is True

    def test_percentage_deadband_suppresses_small_relative_changes(self) -> None:
        deadband_filter = DeadbandFilter()
        deadband = DeadbandConfig("percentage", 5.0)  # 5%
        deadband_filter.should_publish(self._tag(100.0), deadband)
        assert deadband_filter.should_publish(self._tag(104.0), deadband) is False  # 4% change
        assert deadband_filter.should_publish(self._tag(106.0), deadband) is True  # 6% change

    def test_percentage_deadband_from_zero_treats_any_nonzero_as_significant(self) -> None:
        deadband_filter = DeadbandFilter()
        deadband = DeadbandConfig("percentage", 5.0)
        deadband_filter.should_publish(self._tag(0.0), deadband)
        assert deadband_filter.should_publish(self._tag(0.001), deadband) is True

    def test_no_deadband_configured_publishes_on_any_change(self) -> None:
        deadband_filter = DeadbandFilter()
        deadband_filter.should_publish(self._tag(1.0), None)
        assert deadband_filter.should_publish(self._tag(1.0), None) is False
        assert deadband_filter.should_publish(self._tag(1.0000001), None) is True

    def test_separate_tags_tracked_independently(self) -> None:
        deadband_filter = DeadbandFilter()
        deadband = DeadbandConfig("absolute", 5.0)
        deadband_filter.should_publish(self._tag(100.0, tag_id="a"), deadband)
        # "b" has never been seen -> first value -> always published
        assert deadband_filter.should_publish(self._tag(100.0, tag_id="b"), deadband) is True

    def test_non_numeric_values_use_exact_equality(self) -> None:
        deadband_filter = DeadbandFilter()
        deadband = DeadbandConfig("absolute", 5.0)
        tag1 = replace(self._tag("RUNNING"), data_type="STRING")
        deadband_filter.should_publish(tag1, deadband)
        tag2 = replace(tag1, value="STOPPED")
        assert deadband_filter.should_publish(tag2, deadband) is True
        tag3 = replace(tag1, value="STOPPED")
        assert deadband_filter.should_publish(tag3, deadband) is False


class TestBuildTagPipelineConfigs:
    def test_builds_scaling_deadband_and_unit_per_tag(self) -> None:
        from xedge.core.pipeline import build_tag_pipeline_configs

        drivers = [
            {
                "id": "modbus_tcp_01",
                "type": "modbus_tcp",
                "tag_groups": [
                    {
                        "id": "analog",
                        "scan_rate_ms": 1000,
                        "deadband": {"type": "absolute", "value": 0.5},
                        "tags": [
                            {
                                "id": "temperature_01",
                                "function_code": "read_holding_registers",
                                "address": 0,
                                "scaling": {"scale": 0.1, "offset": -273.15},
                                "engineering_unit": "°C",
                            },
                            {
                                "id": "unscaled_tag",
                                "function_code": "read_holding_registers",
                                "address": 1,
                            },
                        ],
                    }
                ],
            }
        ]
        configs = build_tag_pipeline_configs(drivers)

        scaled = configs["modbus_tcp_01/temperature_01"]
        assert scaled.scaling is not None
        assert scaled.scaling.scale == 0.1
        assert scaled.scaling.offset == -273.15
        assert scaled.engineering_unit == "°C"
        assert scaled.deadband is not None
        assert scaled.deadband.kind == "absolute"
        assert scaled.deadband.value == 0.5

        unscaled = configs["modbus_tcp_01/unscaled_tag"]
        assert unscaled.scaling is None
        assert unscaled.engineering_unit is None
        assert unscaled.deadband is not None  # inherited from the tag group

    def test_no_tag_groups_returns_empty(self) -> None:
        from xedge.core.pipeline import build_tag_pipeline_configs

        assert build_tag_pipeline_configs([{"id": "d1", "type": "modbus_tcp"}]) == {}


class TestBuildTagPipelineConfigsIsDriverTypeAgnostic:
    """XEDGE-426. An earlier review flagged this function as "Modbus-shaped
    only" based on its docstring, which said exactly that. On inspection the
    function itself branches on nothing driver-type-specific — it was
    already correct for every driver type whose schema uses the common
    `scaling`/`deadband`/`engineering_unit` shape, which per
    config/schema/drivers/*.schema.json is all three shipped types. The bug
    was the docstring, not the function; these tests are the regression
    coverage for the function actually having this property, since a
    docstring alone can drift out of sync with the code again exactly the
    way the previous one did.
    """

    def test_opcua_client_tags_get_scaling_and_deadband(self) -> None:
        from xedge.core.pipeline import build_tag_pipeline_configs

        drivers = [
            {
                "id": "opcua_1",
                "type": "opcua_client",
                "tag_groups": [
                    {
                        "id": "g1",
                        "scan_rate_ms": 500,
                        "deadband": {"type": "absolute", "value": 0.5},
                        "tags": [
                            {
                                "id": "temp",
                                "node_id": "ns=2;i=1",
                                "scaling": {"scale": 0.1, "offset": 2.0},
                                "engineering_unit": "C",
                            }
                        ],
                    }
                ],
            }
        ]

        config = build_tag_pipeline_configs(drivers)["opcua_1/temp"]

        assert config.scaling is not None
        assert (config.scaling.scale, config.scaling.offset) == (0.1, 2.0)
        assert config.engineering_unit == "C"
        assert config.deadband is not None
        assert (config.deadband.kind, config.deadband.value) == ("absolute", 0.5)

    def test_bacnet_ip_tags_get_scaling_and_deadband(self) -> None:
        from xedge.core.pipeline import build_tag_pipeline_configs

        drivers = [
            {
                "id": "bacnet_1",
                "type": "bacnet_ip",
                "tag_groups": [
                    {
                        "id": "g1",
                        "scan_rate_ms": 1000,
                        "deadband": {"type": "percentage", "value": 2.0},
                        "tags": [
                            {
                                "id": "pressure",
                                "device_address": "192.168.1.60",
                                "object_identifier": "analog-input,1",
                                "scaling": {"scale": 2.0, "offset": 0.0},
                                "engineering_unit": "kPa",
                            }
                        ],
                    }
                ],
            }
        ]

        config = build_tag_pipeline_configs(drivers)["bacnet_1/pressure"]

        assert config.scaling is not None
        assert config.scaling.scale == 2.0
        assert config.engineering_unit == "kPa"
        assert config.deadband is not None
        assert config.deadband.kind == "percentage"

    def test_mixed_fleet_of_all_three_driver_types_in_one_call(self) -> None:
        """The realistic case: one xedge.yaml with several protocols
        configured together, not one driver type in isolation."""
        from xedge.core.pipeline import build_tag_pipeline_configs

        drivers = [
            {
                "id": "modbus_1",
                "type": "modbus_tcp",
                "tag_groups": [
                    {
                        "id": "g1",
                        "scan_rate_ms": 100,
                        "tags": [
                            {
                                "id": "t1",
                                "function_code": "read_holding_registers",
                                "address": 0,
                                "scaling": {"scale": 1.0, "offset": 0.0},
                            }
                        ],
                    }
                ],
            },
            {
                "id": "opcua_1",
                "type": "opcua_client",
                "tag_groups": [
                    {
                        "id": "g1",
                        "scan_rate_ms": 500,
                        "tags": [{"id": "t1", "node_id": "ns=2;i=1", "engineering_unit": "V"}],
                    }
                ],
            },
            {
                "id": "bacnet_1",
                "type": "bacnet_ip",
                "tag_groups": [
                    {
                        "id": "g1",
                        "scan_rate_ms": 1000,
                        "tags": [
                            {
                                "id": "t1",
                                "device_address": "10.0.0.5",
                                "object_identifier": "binary-input,1",
                            }
                        ],
                    }
                ],
            },
        ]

        configs = build_tag_pipeline_configs(drivers)

        assert set(configs) == {"modbus_1/t1", "opcua_1/t1", "bacnet_1/t1"}
        assert configs["opcua_1/t1"].engineering_unit == "V"
