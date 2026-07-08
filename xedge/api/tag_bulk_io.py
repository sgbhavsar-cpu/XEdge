"""CSV/JSON bulk tag import/export (Sprint 31, XEDGE-226/227): lets an OT
engineer commission many tags on an existing tag group at once via a
spreadsheet-friendly CSV, or round-trip a tag group's tags through JSON.
CSV column names mirror `xedge.api.schema_forms`'s dot-path convention
exactly (e.g. "scaling.scale") so the same `unflatten()` the one-tag-at-a-
time form already uses parses a CSV row identically — no separate parsing
logic to keep in sync with a driver type's own tag schema.

Scoped to a single tag group (not a cross-driver universal format): every
driver type's tag shape is different (Modbus's function_code/address vs.
OPC UA's node_id vs. BACnet's device_address/object_type/object_instance),
so a bulk import targets one already-selected driver+tag-group, the same
granularity every other tag_group/tag route in xedge.api.config_ui already
uses — validation and the actual write happen there, against that group's
own driver-type schema, not in this module.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from xedge.api.schema_forms import unflatten


class TagBulkParseError(Exception):
    """Raised for a malformed CSV/JSON payload, or an individual row/entry
    that doesn't parse into a tag dict with an `id` — caught by the route
    layer and reported before anything is written to config."""


def _field_names(schema: dict[str, Any], prefix: str = "") -> list[str]:
    names: list[str] = []
    for name, sub in schema.get("properties", {}).items():
        if sub.get("type") == "object":
            names.extend(_field_names(sub, prefix=f"{prefix}{name}."))
        else:
            names.append(f"{prefix}{name}")
    return names


def _flatten(schema: dict[str, Any], value: dict[str, Any], prefix: str = "") -> dict[str, str]:
    row: dict[str, str] = {}
    for name, sub in schema.get("properties", {}).items():
        raw = (value or {}).get(name)
        if sub.get("type") == "object":
            row.update(_flatten(sub, raw or {}, prefix=f"{prefix}{name}."))
        else:
            row[f"{prefix}{name}"] = "" if raw is None else str(raw)
    return row


def tags_to_csv(tags: list[dict[str, Any]], tag_schema: dict[str, Any]) -> str:
    """Export a tag group's `tags` list as CSV text, one row per tag,
    columns in schema-declaration order — an OT engineer's own spreadsheet
    tool, not a machine-only format."""
    fieldnames = _field_names(tag_schema)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for tag in tags:
        writer.writerow(_flatten(tag_schema, tag))
    return buffer.getvalue()


def tags_from_csv(csv_text: str, tag_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse CSV text (as produced by `tags_to_csv`, or hand-authored by an
    OT engineer using the same column names) into a list of tag dicts."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise TagBulkParseError("CSV has no header row")
    tags: list[dict[str, Any]] = []
    for line_number, row in enumerate(reader, start=2):  # header is line 1
        clean_row = {k: (v or "") for k, v in row.items() if k is not None}
        try:
            tag = unflatten(clean_row, tag_schema)
        except (ValueError, TypeError) as exc:
            raise TagBulkParseError(f"Row {line_number}: {exc}") from exc
        if not tag.get("id"):
            raise TagBulkParseError(f"Row {line_number}: missing required 'id'")
        tags.append(tag)
    return tags


def tags_from_json(json_text: str) -> list[dict[str, Any]]:
    """Parse a JSON array of tag objects (already nested, e.g.
    `{"id": "t1", "scaling": {"scale": 0.1}}`) — JSON needs no flattening,
    unlike CSV, since it natively expresses nested objects."""
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise TagBulkParseError(f"Invalid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise TagBulkParseError("JSON must be an array of tag objects")
    for entry in parsed:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise TagBulkParseError("Each JSON entry must be an object with a non-empty 'id'")
    return parsed
