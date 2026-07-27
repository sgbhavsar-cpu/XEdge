from __future__ import annotations

from pathlib import Path

import pytest

from xedge.core.config import ConfigStore
from xedge.core.main import DEFAULT_DATA_DIR, DataPaths, parse_args


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


class TestDataPaths:
    """XEDGE-402. `/data` used to be hardcoded independently at four call
    sites, so relocating persistent state meant overriding several unrelated
    settings — and missing one silently wrote to the production default.
    """

    def test_defaults_place_everything_under_the_data_root(self) -> None:
        paths = DataPaths(Path("/srv/xedge"))
        assert paths.store == Path("/srv/xedge/store")
        assert paths.config_history == Path("/srv/xedge/config-history")
        assert paths.webui == Path("/srv/xedge/webui")
        assert paths.fleet_device_token == Path("/srv/xedge/fleet/device_token")
        assert paths.mqtt_broker_password_file == Path("/srv/xedge/mqtt-broker/passwords")

    def test_one_setting_relocates_every_persistent_path(self, tmp_path: Path) -> None:
        """The exact regression: overriding only the data root must move the
        config history too. Leaving it behind is what made four integration
        tests fail in CI with PermissionError: '/data'."""
        store = ConfigStore({"schema_version": "0.1", "data_dir": str(tmp_path)})
        paths = DataPaths.from_config(store)

        for path in (paths.store, paths.config_history, paths.webui, paths.fleet):
            assert tmp_path in path.parents or path == tmp_path

    def test_data_dir_defaults_to_slash_data(self) -> None:
        paths = DataPaths.from_config(ConfigStore({"schema_version": "0.1"}))
        assert paths.data_dir == DEFAULT_DATA_DIR
        assert paths.store == Path("/data/store")
        assert paths.config_history == Path("/data/config-history")

    def test_individual_paths_still_override_the_root(self, tmp_path: Path) -> None:
        """Deployments that genuinely split state across volumes (config
        history on internal flash, telemetry on a removable SD card) keep
        working."""
        store = ConfigStore(
            {
                "schema_version": "0.1",
                "data_dir": str(tmp_path),
                "store": {"directory": "/mnt/sdcard/telemetry"},
                "config_management": {"history_directory": "/mnt/flash/history"},
            }
        )
        paths = DataPaths.from_config(store)

        assert paths.store == Path("/mnt/sdcard/telemetry")
        assert paths.config_history == Path("/mnt/flash/history")
        # Paths without their own setting still follow the root.
        assert paths.webui == tmp_path / "webui"
