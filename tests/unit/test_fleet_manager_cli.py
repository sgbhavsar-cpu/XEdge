from __future__ import annotations

import json
from pathlib import Path

from xedge.fleet.manager_cli import _default_schema_path, parse_args


def test_default_schema_path_resolves_to_a_real_schema_file() -> None:
    path = _default_schema_path()

    assert path.is_file()
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["type"] == "object"


def test_parse_args_defaults() -> None:
    args = parse_args(["--data-dir", "/tmp/fleet"])

    assert args.port == 8090
    assert args.device_port == 8091
    assert args.schema_path is None
    assert args.identity_hostname == "xedge-fleet-manager"


def test_parse_args_schema_path_override() -> None:
    args = parse_args(["--schema-path", "/custom/schema.json"])

    assert args.schema_path == Path("/custom/schema.json")
