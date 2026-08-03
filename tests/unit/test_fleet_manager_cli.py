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
    assert args.database_url == "postgresql+asyncpg://xedge:xedge@localhost:5432/xedge_fleet"


def test_parse_args_database_url_override() -> None:
    args = parse_args(["--database-url", "postgresql+asyncpg://u:p@db-host:5432/mydb"])

    assert args.database_url == "postgresql+asyncpg://u:p@db-host:5432/mydb"


def test_parse_args_schema_path_override() -> None:
    args = parse_args(["--schema-path", "/custom/schema.json"])

    assert args.schema_path == Path("/custom/schema.json")
