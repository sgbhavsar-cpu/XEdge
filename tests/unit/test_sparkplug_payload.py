"""Unit + oracle tests for the in-house Sparkplug B Payload/Metric encoder.

Oracle cross-validation: decode our encoded bytes with pysparkplug's
`NBirth`/`NData` classes, which wrap the officially generated protobuf
classes (Apache-2.0) — an independent implementation of the same public
spec, used strictly as a black-box decoder here (ADR-006 pattern).
"""

from __future__ import annotations

import pytest
from pysparkplug._payload import NBirth, NData

from xedge.northbound.sparkplug.payload import (
    DataType,
    SparkplugMetric,
    encode_payload,
    infer_datatype,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, DataType.BOOLEAN),
        (False, DataType.BOOLEAN),
        (42, DataType.UINT32),
        (3.14, DataType.DOUBLE),
        ("hello", DataType.STRING),
    ],
)
def test_infer_datatype(value: object, expected: DataType) -> None:
    assert infer_datatype(value) == expected  # type: ignore[arg-type]


def test_infer_datatype_bool_before_int() -> None:
    # bool is an int subclass in Python; must resolve to BOOLEAN, not UINT32.
    assert infer_datatype(True) == DataType.BOOLEAN


def test_infer_datatype_rejects_bytes() -> None:
    with pytest.raises(TypeError):
        infer_datatype(b"\x01\x02")


def test_encode_metric_with_null_sets_is_null_flag() -> None:
    metric = SparkplugMetric(
        name="t", timestamp_ms=1000, datatype=DataType.UINT32, value=None, is_null=True
    )
    payload = encode_payload(timestamp_ms=1000, seq=0, metrics=[metric])
    decoded = NBirth.decode(payload)
    assert decoded.metrics[0].is_null is True


class TestOracleCrossValidation:
    """Decode our own encoder's output with pysparkplug's official-spec
    generated protobuf classes — proves wire compatibility (ADR-006)."""

    def test_holding_register_value_roundtrip(self) -> None:
        metric = SparkplugMetric(
            name="modbus_tcp_01/temperature_01",
            timestamp_ms=1700000000123,
            datatype=DataType.UINT32,
            value=12345,
        )
        payload = encode_payload(timestamp_ms=1700000000123, seq=5, metrics=[metric])
        decoded = NData.decode(payload)

        assert decoded.seq == 5
        assert decoded.timestamp == 1700000000123
        assert len(decoded.metrics) == 1
        decoded_metric = decoded.metrics[0]
        assert decoded_metric.name == "modbus_tcp_01/temperature_01"
        assert decoded_metric.value == 12345
        assert int(decoded_metric.datatype) == int(DataType.UINT32)

    def test_boolean_value_roundtrip(self) -> None:
        metric = SparkplugMetric(
            name="pump_running", timestamp_ms=1700000000000, datatype=DataType.BOOLEAN, value=True
        )
        payload = encode_payload(timestamp_ms=1700000000000, seq=0, metrics=[metric])
        decoded = NData.decode(payload)
        assert decoded.metrics[0].value is True

    def test_double_value_roundtrip(self) -> None:
        metric = SparkplugMetric(
            name="temp_scaled", timestamp_ms=1700000000000, datatype=DataType.DOUBLE, value=85.3
        )
        payload = encode_payload(timestamp_ms=1700000000000, seq=0, metrics=[metric])
        decoded = NData.decode(payload)
        assert decoded.metrics[0].value == pytest.approx(85.3)

    def test_string_value_roundtrip(self) -> None:
        metric = SparkplugMetric(
            name="status", timestamp_ms=1700000000000, datatype=DataType.STRING, value="OK"
        )
        payload = encode_payload(timestamp_ms=1700000000000, seq=0, metrics=[metric])
        decoded = NData.decode(payload)
        assert decoded.metrics[0].value == "OK"

    def test_multiple_metrics_in_one_payload_roundtrip(self) -> None:
        metrics = [
            SparkplugMetric(name="a", timestamp_ms=1, datatype=DataType.UINT32, value=1),
            SparkplugMetric(name="b", timestamp_ms=2, datatype=DataType.BOOLEAN, value=False),
            SparkplugMetric(name="c", timestamp_ms=3, datatype=DataType.DOUBLE, value=1.5),
        ]
        payload = encode_payload(timestamp_ms=100, seq=1, metrics=metrics)
        decoded = NData.decode(payload)
        assert [m.name for m in decoded.metrics] == ["a", "b", "c"]
        assert decoded.metrics[0].value == 1
        assert decoded.metrics[1].value is False
        assert decoded.metrics[2].value == pytest.approx(1.5)

    def test_birth_certificate_with_bdseq_roundtrip(self) -> None:
        metrics = [
            SparkplugMetric(name="bdSeq", timestamp_ms=1000, datatype=DataType.UINT64, value=0),
            SparkplugMetric(
                name="modbus_tcp_01/temperature_01",
                timestamp_ms=1000,
                datatype=DataType.UINT32,
                value=0,
            ),
        ]
        payload = encode_payload(timestamp_ms=1000, seq=0, metrics=metrics)
        decoded = NBirth.decode(payload)
        assert decoded.seq == 0
        assert decoded.metrics[0].name == "bdSeq"
        assert decoded.metrics[0].value == 0

    def test_alias_roundtrip(self) -> None:
        metric = SparkplugMetric(
            name="temperature_01", timestamp_ms=1000, datatype=DataType.UINT32, value=42, alias=7
        )
        payload = encode_payload(timestamp_ms=1000, seq=0, metrics=[metric])
        decoded = NData.decode(payload)
        assert decoded.metrics[0].alias == 7
