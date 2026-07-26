"""Schema-driven config form engine (ADR-007 follow-up, FR-WU-003/010): reads
a JSON Schema fragment plus the current data value and produces a tree of
`FieldDescriptor` objects that Jinja2 templates render recursively — one
control type per JSON Schema type/enum/x-secret marker, so a new driver-type
schema or field automatically gets a working form with zero new template
code (per the schema-driven design decision).

Each tree node in the config UI (a core section, one driver, one tag group,
one tag) is edited as its *own* flat form containing only that node's own
scalar/object fields — child collections (a driver's tag_groups, a tag
group's tags) are rendered as a separate navigable list by the route layer,
not inlined into the parent's form. This keeps every form's field-name
nesting shallow (dot-path for a nested object like `scaling.scale`, no
array-index bracket names to parse), which is what makes plain HTML forms
(no JS framework, per ADR-007) practical for an arbitrarily nested schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ControlType = Literal["text", "number", "checkbox", "select", "object", "password"]

_LABEL_FIXUPS = {
    "id": "ID",
    "url": "URL",
    "ms": "(ms)",
    "mqtt": "MQTT",
    "qos": "QoS",
    "sd": "SD",
}


@dataclass
class FieldDescriptor:
    name: str
    """Form field name — a dot-path for nested objects, e.g. "scaling.scale"."""
    label: str
    control: ControlType
    value: Any = None
    required: bool = False
    description: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: str = "1"
    options: list[Any] = field(default_factory=list)
    children: list[FieldDescriptor] = field(default_factory=list)
    """Populated only when control == "object": the nested fields."""
    suggestions_endpoint: str | None = None
    """Set from the schema's `x-suggestions-endpoint` (XEDGE-434) — a REST
    GET endpoint returning `list[str]` the client fetches once, on page
    load, to populate an HTML `<datalist>` bound to this text input. Unlike
    `enum` (a fixed, schema-known set of values), this is for a set only
    knowable at runtime on the *device itself* — e.g. which serial ports are
    physically present right now — so the operator can still type a value
    the endpoint didn't happen to detect (a port that appears after the
    list loaded, one on an unusual path) rather than being locked out of it
    the way a `<select>` would."""


def humanize(key: str) -> str:
    """snake_case -> Title Case, e.g. "scan_rate_ms" -> "Scan Rate (ms)"."""
    words = key.replace("_", " ").split(" ")
    return " ".join(_LABEL_FIXUPS.get(w.lower(), w.capitalize()) for w in words)


def build_field(
    name: str, schema: dict[str, Any], value: Any, required: bool = False
) -> FieldDescriptor:
    """Build one FieldDescriptor for `schema` (a single property's schema
    fragment) given the property's current `value` (may be None)."""
    label = schema.get("title") or humanize(name.rsplit(".", 1)[-1])
    description = schema.get("description")
    schema_type = schema.get("type")

    if schema.get("x-secret"):
        # FR-WU-006: never render the current (possibly resolved) value —
        # an empty field means "leave unchanged" (see config_ui's write path).
        return FieldDescriptor(
            name=name,
            label=label,
            control="password",
            value="",
            required=False,
            description=description or "Leave blank to keep the current value unchanged.",
        )

    if "enum" in schema:
        resolved = value if value is not None else schema.get("default")
        return FieldDescriptor(
            name=name,
            label=label,
            control="select",
            value=resolved,
            required=required,
            description=description,
            options=list(schema["enum"]),
        )

    if schema_type == "boolean":
        resolved_bool = value if value is not None else schema.get("default", False)
        return FieldDescriptor(
            name=name,
            label=label,
            control="checkbox",
            value=bool(resolved_bool),
            description=description,
        )

    if schema_type in ("integer", "number"):
        minimum = schema.get("minimum")
        if minimum is None and "exclusiveMinimum" in schema:
            minimum = schema["exclusiveMinimum"]
        return FieldDescriptor(
            name=name,
            label=label,
            control="number",
            value=value if value is not None else schema.get("default"),
            required=required,
            description=description,
            minimum=minimum,
            maximum=schema.get("maximum"),
            step="1" if schema_type == "integer" else "any",
        )

    if schema_type == "object":
        return FieldDescriptor(
            name=name,
            label=label,
            control="object",
            required=required,
            description=description,
            children=build_object_fields(schema, value or {}, prefix=f"{name}."),
        )

    # Fall back to a plain text field for "string" and anything untyped.
    return FieldDescriptor(
        name=name,
        label=label,
        control="text",
        value=value if value is not None else schema.get("default", ""),
        required=required,
        description=description,
        suggestions_endpoint=schema.get("x-suggestions-endpoint"),
    )


def build_object_fields(
    schema: dict[str, Any],
    value: dict[str, Any],
    skip: frozenset[str] = frozenset(),
    prefix: str = "",
) -> list[FieldDescriptor]:
    """Build FieldDescriptors for an object schema's own properties, in
    schema-declaration order, excluding any in `skip` — used to leave out a
    node's child-collection properties (e.g. a driver's `tag_groups`),
    which the route layer renders separately as a navigable list rather
    than inlining into this form. `prefix` is prepended to each field's
    dot-path name — set when recursing into a nested object (see
    build_field's "object" branch) so e.g. "value" becomes "deadband.value",
    matching unflatten()'s expectations."""
    required_names = frozenset(schema.get("required", []))
    properties = schema.get("properties", {})
    return [
        build_field(
            f"{prefix}{child_name}",
            child_schema,
            value.get(child_name),
            required=child_name in required_names,
        )
        for child_name, child_schema in properties.items()
        if child_name not in skip
    ]


def unflatten(
    form_data: dict[str, str], schema: dict[str, Any], skip: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Reconstruct a nested dict from a flat {"scaling.scale": "1.0", ...}
    form submission, converting each value to the type its schema fragment
    declares (numbers/booleans parsed, empty optional strings dropped).
    Mirrors build_object_fields' dot-path naming and `skip` exactly — pass
    the same `skip` set used to render the form the data came from, so a
    property this schema declares but that form never showed (e.g. an
    immutable "id", or a child collection like "tags") is never parsed out
    of unrelated form fields."""
    result: dict[str, Any] = {}
    properties = schema.get("properties", {})
    required_names = frozenset(schema.get("required", []))
    for prop_name, prop_schema in properties.items():
        if prop_name in skip:
            continue
        prefix = f"{prop_name}."
        if prop_schema.get("type") == "object":
            nested_data = {
                key[len(prefix) :]: val for key, val in form_data.items() if key.startswith(prefix)
            }
            if not nested_data:
                continue
            nested = unflatten(nested_data, prop_schema)
            if nested:
                result[prop_name] = nested
            continue

        if prop_name not in form_data:
            if prop_schema.get("type") == "boolean":
                # HTML forms simply omit an unchecked checkbox — for a
                # boolean field that IS part of this submitted form (every
                # boolean the route renders becomes a checkbox), absence
                # means "unchecked", i.e. explicit False, not "not set".
                result[prop_name] = False
            continue
        raw_value = form_data[prop_name]

        if prop_schema.get("x-secret"):
            if raw_value:  # blank means "leave unchanged" — caller merges over the prior value
                result[prop_name] = raw_value
            continue

        if raw_value == "" and prop_name not in required_names:
            continue  # an empty optional field means "not set", not the literal string ""

        result[prop_name] = _coerce(raw_value, prop_schema)
    return result


def _coerce(raw_value: str, schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if schema_type == "boolean":
        return raw_value in ("true", "on", "1", "yes")
    if schema_type == "integer":
        return int(raw_value)
    if schema_type == "number":
        return float(raw_value)
    if "enum" in schema and schema["enum"] and isinstance(schema["enum"][0], int):
        return int(raw_value)
    return raw_value
