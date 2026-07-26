from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xedge.api.schema_forms import (
    FieldDescriptor,
    build_field,
    build_object_fields,
    humanize,
    unflatten,
)
from xedge.drivers.modbus.datatypes import DATA_TYPE_NAMES


class TestHumanize:
    def test_simple_word(self) -> None:
        assert humanize("host") == "Host"

    def test_snake_case(self) -> None:
        assert humanize("scan_rate_ms") == "Scan Rate (ms)"

    def test_known_acronym(self) -> None:
        assert humanize("node_id") == "Node ID"
        assert humanize("endpoint_url") == "Endpoint URL"


class TestBuildFieldScalarTypes:
    def test_string_field(self) -> None:
        f = build_field("host", {"type": "string", "minLength": 1}, "127.0.0.1")
        assert f.control == "text"
        assert f.value == "127.0.0.1"

    def test_string_field_uses_schema_default_when_value_is_none(self) -> None:
        f = build_field("client_id", {"type": "string", "default": ""}, None)
        assert f.value == ""

    def test_boolean_field(self) -> None:
        f = build_field("enabled", {"type": "boolean", "default": True}, False)
        assert f.control == "checkbox"
        assert f.value is False

    def test_boolean_field_falls_back_to_default_when_unset(self) -> None:
        f = build_field("enabled", {"type": "boolean", "default": True}, None)
        assert f.value is True

    def test_integer_field_with_bounds(self) -> None:
        f = build_field("port", {"type": "integer", "minimum": 1, "maximum": 65535}, 502)
        assert f.control == "number"
        assert f.value == 502
        assert f.minimum == 1
        assert f.maximum == 65535
        assert f.step == "1"

    def test_number_field_uses_exclusive_minimum_as_minimum_hint(self) -> None:
        f = build_field("connect_timeout_seconds", {"type": "number", "exclusiveMinimum": 0}, 5.0)
        assert f.control == "number"
        assert f.minimum == 0
        assert f.step == "any"

    def test_enum_field_becomes_select(self) -> None:
        f = build_field(
            "level",
            {"type": "string", "enum": ["DEBUG", "INFO", "WARNING"], "default": "INFO"},
            "DEBUG",
        )
        assert f.control == "select"
        assert f.value == "DEBUG"
        assert f.options == ["DEBUG", "INFO", "WARNING"]

    def test_enum_field_falls_back_to_default(self) -> None:
        f = build_field("level", {"type": "string", "enum": ["A", "B"], "default": "B"}, None)
        assert f.value == "B"

    def test_required_flag_propagates(self) -> None:
        f = build_field("host", {"type": "string"}, "x", required=True)
        assert f.required is True


class TestBuildFieldSecretMasking:
    def test_secret_field_is_password_control_and_always_empty(self) -> None:
        f = build_field("password", {"type": "string", "x-secret": True}, "some-resolved-value")
        assert f.control == "password"
        assert f.value == ""

    def test_secret_field_never_marked_required_regardless_of_schema(self) -> None:
        f = build_field("password", {"type": "string", "x-secret": True}, None, required=True)
        assert f.required is False


class TestBuildFieldSuggestionsEndpoint:
    """XEDGE-434 — same custom-`x-`-keyword mechanism as `x-secret` above,
    for a field whose valid values are only knowable at runtime on the
    device itself (e.g. which serial ports are physically present)."""

    def test_carries_the_endpoint_through_to_the_field(self) -> None:
        f = build_field(
            "port", {"type": "string", "x-suggestions-endpoint": "/api/v1/serial-ports"}, None
        )
        assert f.control == "text"
        assert f.suggestions_endpoint == "/api/v1/serial-ports"

    def test_defaults_to_none_when_the_schema_has_no_such_keyword(self) -> None:
        f = build_field("host", {"type": "string"}, "127.0.0.1")
        assert f.suggestions_endpoint is None

    def test_the_field_stays_freely_editable_not_a_select(self) -> None:
        """Unlike `enum`, this must never turn the field into a `<select>` —
        the whole point is that an operator can still type a value the
        detection endpoint didn't happen to find."""
        f = build_field(
            "port", {"type": "string", "x-suggestions-endpoint": "/api/v1/serial-ports"}, None
        )
        assert f.control != "select"


class TestBuildFieldNestedObject:
    def test_object_field_recurses_into_children(self) -> None:
        schema = {
            "type": "object",
            "required": ["type", "value"],
            "properties": {
                "type": {"type": "string", "enum": ["absolute", "percentage"]},
                "value": {"type": "number", "minimum": 0},
            },
        }
        f = build_field("deadband", schema, {"type": "percentage", "value": 0.5})
        assert f.control == "object"
        assert len(f.children) == 2
        by_name = {c.name: c for c in f.children}
        assert by_name["deadband.type"].control == "select"
        assert by_name["deadband.type"].value == "percentage"
        assert by_name["deadband.value"].control == "number"
        assert by_name["deadband.value"].value == 0.5
        assert by_name["deadband.value"].required is True

    def test_object_field_with_none_value_still_renders_children_with_defaults(self) -> None:
        schema = {
            "type": "object",
            "properties": {"scale": {"type": "number", "default": 1.0}},
        }
        f = build_field("scaling", schema, None)
        assert f.children[0].value == 1.0


class TestBuildObjectFields:
    def test_builds_one_descriptor_per_property(self) -> None:
        schema = {
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer", "default": 502},
            }
        }
        fields = build_object_fields(schema, {"host": "10.0.0.1"})
        names = [f.name for f in fields]
        assert names == ["host", "port"]
        assert fields[0].value == "10.0.0.1"
        assert fields[1].value == 502

    def test_skip_excludes_named_properties(self) -> None:
        schema = {
            "properties": {
                "id": {"type": "string"},
                "tag_groups": {"type": "array", "items": {"type": "object"}},
            }
        }
        fields = build_object_fields(schema, {"id": "d1"}, skip=frozenset({"tag_groups"}))
        assert [f.name for f in fields] == ["id"]

    def test_required_names_marked_on_children(self) -> None:
        schema = {
            "required": ["host"],
            "properties": {"host": {"type": "string"}, "port": {"type": "integer"}},
        }
        fields = build_object_fields(schema, {})
        by_name = {f.name: f for f in fields}
        assert by_name["host"].required is True
        assert by_name["port"].required is False


class TestUnflatten:
    def test_flat_scalars(self) -> None:
        schema = {
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer"},
                "enabled": {"type": "boolean"},
            }
        }
        result = unflatten({"host": "10.0.0.1", "port": "502", "enabled": "on"}, schema)
        assert result == {"host": "10.0.0.1", "port": 502, "enabled": True}

    def test_unchecked_checkbox_absent_from_form_data_is_treated_as_explicit_false(self) -> None:
        # HTML forms simply omit an unchecked checkbox on submit — without
        # this, saving a form with a checkbox unchecked would silently
        # leave the field unset (falling back to its schema default,
        # often True) instead of persisting the intended False.
        schema = {"properties": {"enabled": {"type": "boolean"}}}
        result = unflatten({}, schema)
        assert result == {"enabled": False}

    def test_checked_checkbox_present_in_form_data_is_true(self) -> None:
        schema = {"properties": {"enabled": {"type": "boolean"}}}
        result = unflatten({"enabled": "on"}, schema)
        assert result == {"enabled": True}

    def test_nested_object_dot_path(self) -> None:
        schema = {
            "properties": {
                "deadband": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "value": {"type": "number"},
                    },
                }
            }
        }
        result = unflatten({"deadband.type": "absolute", "deadband.value": "2.5"}, schema)
        assert result == {"deadband": {"type": "absolute", "value": 2.5}}

    def test_nested_object_entirely_absent_is_omitted(self) -> None:
        schema = {
            "properties": {
                "scaling": {
                    "type": "object",
                    "properties": {"scale": {"type": "number"}},
                }
            }
        }
        assert unflatten({}, schema) == {}

    def test_empty_optional_string_is_omitted_not_empty_string(self) -> None:
        schema = {"properties": {"engineering_unit": {"type": "string"}}}
        assert unflatten({"engineering_unit": ""}, schema) == {}

    def test_empty_required_string_is_kept_as_empty_string(self) -> None:
        # lets schema validation (which the caller runs afterward) produce
        # the real "required field missing/empty" error, rather than
        # unflatten silently swallowing it.
        schema = {"required": ["host"], "properties": {"host": {"type": "string"}}}
        assert unflatten({"host": ""}, schema) == {"host": ""}

    def test_secret_field_blank_is_omitted_so_caller_can_preserve_prior_value(self) -> None:
        schema = {"properties": {"password": {"type": "string", "x-secret": True}}}
        assert unflatten({"password": ""}, schema) == {}

    def test_secret_field_with_new_value_is_included(self) -> None:
        schema = {"properties": {"password": {"type": "string", "x-secret": True}}}
        assert unflatten({"password": "${SECRET:new_name}"}, schema) == {
            "password": "${SECRET:new_name}"
        }

    def test_integer_enum_coerced_to_int(self) -> None:
        schema = {"properties": {"data_bits": {"type": "integer", "enum": [7, 8]}}}
        assert unflatten({"data_bits": "8"}, schema) == {"data_bits": 8}

    def test_skip_ignores_field_even_if_present_in_form_data(self) -> None:
        # Mirrors build_object_fields' skip — a form that never rendered
        # "id" (an immutable identifier) shouldn't have it parsed out even
        # if a value happens to be present, e.g. from a stale/hand-crafted
        # submission.
        schema = {"properties": {"id": {"type": "string"}, "scan_rate_ms": {"type": "integer"}}}
        result = unflatten(
            {"id": "should-be-ignored", "scan_rate_ms": "500"}, schema, skip=frozenset({"id"})
        )
        assert result == {"scan_rate_ms": 500}


class TestSprintC1FieldsRenderFromSchema:
    """XEDGE-415. The Web UI form generator is schema-driven (ADR-007), so
    adding the Sprint C1 settings to the driver schemas should surface them in
    the config editor with no template work. These tests lock that in — a
    future schema edit that drops a field would otherwise silently remove it
    from the UI with every test still passing.
    """

    @staticmethod
    def _modbus_schema() -> dict[str, Any]:
        path = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "schema"
            / "drivers"
            / "modbus_tcp.schema.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def _fields(self, section: str) -> dict[str, FieldDescriptor]:
        schema = self._modbus_schema()
        group = schema["properties"]["tag_groups"]["items"]
        target = {
            "tag": group["properties"]["tags"]["items"],
            "group": group,
            "config": schema["properties"]["config"],
        }[section]
        return {field.name: field for field in build_object_fields(target, {})}

    def test_data_type_is_a_dropdown_of_the_supported_widths(self) -> None:
        field = self._fields("tag")["data_type"]
        assert field.control == "select"
        assert field.options is not None
        assert set(field.options) == set(DATA_TYPE_NAMES)

    def test_word_and_byte_order_are_dropdowns(self) -> None:
        fields = self._fields("tag")
        for name in ("word_order", "byte_order"):
            assert fields[name].control == "select"
            assert fields[name].options == ["big", "little"]

    def test_batching_controls_appear_on_the_tag_group_form(self) -> None:
        fields = self._fields("group")
        assert fields["max_block_size"].control == "number"
        assert fields["max_block_gap"].control == "number"

    def test_retry_controls_appear_on_the_driver_config_form(self) -> None:
        fields = self._fields("config")
        assert fields["retry_count"].control == "number"
        assert fields["retry_on_exception"].control == "checkbox"
        assert fields["retry_backoff_seconds"].control == "number"


class TestSprintC2FieldsRenderFromSchema:
    """XEDGE-421/422. Same schema-driven guarantee as
    TestSprintC1FieldsRenderFromSchema, for this sprint's additions."""

    @staticmethod
    def _modbus_schema() -> dict[str, Any]:
        path = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "schema"
            / "drivers"
            / "modbus_tcp.schema.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def _fields(self, section: str) -> dict[str, FieldDescriptor]:
        schema = self._modbus_schema()
        group = schema["properties"]["tag_groups"]["items"]
        target = {
            "tag": group["properties"]["tags"]["items"],
            "config": schema["properties"]["config"],
        }[section]
        return {field.name: field for field in build_object_fields(target, {})}

    def test_access_is_a_dropdown_of_the_three_write_semantics(self) -> None:
        field = self._fields("tag")["access"]
        assert field.control == "select"
        assert field.options == ["read_write", "read_only", "write_only"]

    def test_connectivity_thresholds_appear_on_the_driver_config_form(self) -> None:
        fields = self._fields("config")
        assert fields["consecutive_failure_threshold"].control == "number"
        assert fields["recovery_threshold"].control == "number"


class TestSprintC3FieldsRenderFromSchema:
    """XEDGE-432/434, against the real modbus_rtu_serial schema — the RTS
    delay fields and the port suggestions endpoint."""

    @staticmethod
    def _serial_schema() -> dict[str, Any]:
        path = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "schema"
            / "drivers"
            / "modbus_rtu_serial.schema.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def test_port_field_carries_the_serial_ports_suggestions_endpoint(self) -> None:
        schema = self._serial_schema()
        config_schema = schema["properties"]["config"]
        fields = {f.name: f for f in build_object_fields(config_schema, {})}

        assert fields["port"].control == "text"
        assert fields["port"].suggestions_endpoint == "/api/v1/serial-ports"

    def test_rts_delay_fields_are_numbers_on_the_driver_config_form(self) -> None:
        schema = self._serial_schema()
        config_schema = schema["properties"]["config"]
        fields = {f.name: f for f in build_object_fields(config_schema, {})}

        assert fields["rts_pre_delay_us"].control == "number"
        assert fields["rts_post_delay_us"].control == "number"
