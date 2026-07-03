from __future__ import annotations

from pathlib import Path

import pytest

from xedge.core.main import parse_args


def test_parse_args_requires_config() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_config_path() -> None:
    args = parse_args(["--config", "config/examples/modbus-minimal.yaml"])
    assert args.config == Path("config/examples/modbus-minimal.yaml")
    assert args.schema.name == "xedge-core.schema.json"


def test_parse_args_custom_schema() -> None:
    args = parse_args(["--config", "base.yaml", "--schema", "custom.schema.json"])
    assert args.schema == Path("custom.schema.json")


def test_parse_args_version_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--version"])
    assert exc_info.value.code == 0
