from __future__ import annotations

import json

import pytest

from xedge.api.tag_bulk_io import (
    TagBulkParseError,
    tags_from_csv,
    tags_from_json,
    tags_to_csv,
)

_MODBUS_TAG_SCHEMA = {
    "type": "object",
    "required": ["id", "function_code", "address"],
    "properties": {
        "id": {"type": "string"},
        "function_code": {
            "type": "string",
            "enum": [
                "read_coils",
                "read_discrete_inputs",
                "read_holding_registers",
                "read_input_registers",
            ],
        },
        "address": {"type": "integer"},
        "scaling": {
            "type": "object",
            "properties": {
                "scale": {"type": "number"},
                "offset": {"type": "number"},
            },
        },
        "engineering_unit": {"type": "string"},
    },
}


def test_tags_to_csv_produces_dot_path_header_and_row() -> None:
    tags = [
        {
            "id": "t1",
            "function_code": "read_holding_registers",
            "address": 10,
            "scaling": {"scale": 0.1, "offset": -273.15},
            "engineering_unit": "degC",
        }
    ]
    csv_text = tags_to_csv(tags, _MODBUS_TAG_SCHEMA)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "id,function_code,address,scaling.scale,scaling.offset,engineering_unit"
    assert lines[1] == "t1,read_holding_registers,10,0.1,-273.15,degC"


def test_tags_to_csv_blank_for_missing_optional_fields() -> None:
    tags = [{"id": "t1", "function_code": "read_coils", "address": 0}]
    csv_text = tags_to_csv(tags, _MODBUS_TAG_SCHEMA)
    lines = csv_text.strip().splitlines()
    assert lines[1] == "t1,read_coils,0,,,"


def test_tags_from_csv_round_trips_a_tag() -> None:
    tags = [
        {
            "id": "t1",
            "function_code": "read_holding_registers",
            "address": 10,
            "scaling": {"scale": 0.1, "offset": -273.15},
            "engineering_unit": "degC",
        }
    ]
    csv_text = tags_to_csv(tags, _MODBUS_TAG_SCHEMA)
    parsed = tags_from_csv(csv_text, _MODBUS_TAG_SCHEMA)
    assert parsed == tags


def test_tags_from_csv_parses_multiple_rows() -> None:
    csv_text = "id,function_code,address\nt1,read_coils,0\nt2,read_holding_registers,100\n"
    parsed = tags_from_csv(csv_text, _MODBUS_TAG_SCHEMA)
    assert parsed == [
        {"id": "t1", "function_code": "read_coils", "address": 0},
        {"id": "t2", "function_code": "read_holding_registers", "address": 100},
    ]


def test_tags_from_csv_missing_id_raises_with_row_number() -> None:
    csv_text = "id,function_code,address\n,read_coils,0\n"
    with pytest.raises(TagBulkParseError, match="Row 2"):
        tags_from_csv(csv_text, _MODBUS_TAG_SCHEMA)


def test_tags_from_csv_bad_numeric_value_raises() -> None:
    csv_text = "id,function_code,address\nt1,read_coils,not-a-number\n"
    with pytest.raises(TagBulkParseError, match="Row 2"):
        tags_from_csv(csv_text, _MODBUS_TAG_SCHEMA)


def test_tags_from_csv_no_header_raises() -> None:
    with pytest.raises(TagBulkParseError, match="header"):
        tags_from_csv("", _MODBUS_TAG_SCHEMA)


def test_tags_from_json_parses_nested_objects() -> None:
    payload = json.dumps(
        [{"id": "t1", "function_code": "read_coils", "address": 0, "scaling": {"scale": 2.0}}]
    )
    parsed = tags_from_json(payload)
    assert parsed == [
        {"id": "t1", "function_code": "read_coils", "address": 0, "scaling": {"scale": 2.0}}
    ]


def test_tags_from_json_invalid_json_raises() -> None:
    with pytest.raises(TagBulkParseError, match="Invalid JSON"):
        tags_from_json("not json")


def test_tags_from_json_non_array_raises() -> None:
    with pytest.raises(TagBulkParseError, match="array"):
        tags_from_json(json.dumps({"id": "t1"}))


def test_tags_from_json_entry_missing_id_raises() -> None:
    with pytest.raises(TagBulkParseError, match="id"):
        tags_from_json(json.dumps([{"function_code": "read_coils"}]))
