from __future__ import annotations

from xedge.api.schema_forms import build_field, build_object_fields, humanize, unflatten


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
