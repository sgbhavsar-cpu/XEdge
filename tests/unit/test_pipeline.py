from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xedge.core.pipeline import normalize
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
    assert tag.metadata == {"modbus_exception": None, "request_latency_ms": 12}
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
