from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xedge.api.auth import LoginAttemptTracker, SessionManager, UserStore
from xedge.api.server import create_app
from xedge.core.config import ConfigVersionHistory
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.observability.audit_log import AuditLog
from xedge.store.latest_values import LatestValueStore
from xedge.store.ring_buffer import RingBufferManager


def _build_app(tmp_path: Path, core_schema_path: Path) -> FastAPI:
    config_path = tmp_path / "xedge.yaml"
    if not config_path.is_file():
        config_path.write_text("schema_version: '0.1'\n", encoding="utf-8")
    return create_app(
        DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1)),
        ConfigVersionHistory(tmp_path),
        None,
        user_store=UserStore(tmp_path / "webui" / "users.json"),
        session_manager=SessionManager(secret_key=b"test-secret-key"),
        login_tracker=LoginAttemptTracker(),
        config_path=config_path,
        schema_path=core_schema_path,
        latest_values=LatestValueStore(),
        audit_log=AuditLog(tmp_path / "webui" / "audit.jsonl"),
        ring_buffers=RingBufferManager(),
    )


def _authenticated_client(app: FastAPI) -> TestClient:
    client = TestClient(app)
    client.post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )
    return client


def _current_config(tmp_path: Path) -> dict:
    return yaml.safe_load((tmp_path / "xedge.yaml").read_text(encoding="utf-8"))


def _seed_config(tmp_path: Path, config: dict) -> None:
    """Write directly to xedge.yaml — the file the config UI reads from
    (not ConfigVersionHistory, which only reflects a write after the
    hot-reload poll loop next runs; see config_ui._full_config's
    docstring)."""
    (tmp_path / "xedge.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


class TestCoreSectionForms:
    def test_unauthenticated_get_redirects_to_login(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/ui/config/core/logging")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"

    def test_logging_section_renders_enum_as_select_with_current_value(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        _seed_config(tmp_path, {"schema_version": "0.1", "logging": {"level": "DEBUG"}})
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.get("/ui/config/core/logging")
        assert response.status_code == 200
        assert '<option value="DEBUG" selected>' in response.text

    def test_saving_logging_section_writes_config(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.post("/ui/config/core/logging", data={"level": "WARNING"})
        assert response.status_code == 200
        assert "Saved" in response.text
        assert _current_config(tmp_path)["logging"]["level"] == "WARNING"

    def test_unchecking_boolean_checkbox_persists_as_false(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        _seed_config(tmp_path, {"schema_version": "0.1", "watchdog": {"enabled": True}})
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        # Submitting without "enabled" in the form data simulates an
        # unchecked checkbox (HTML never sends unchecked boxes).
        response = client.post("/ui/config/core/watchdog", data={"interval_seconds": "15"})
        assert response.status_code == 200
        assert _current_config(tmp_path)["watchdog"]["enabled"] is False

    def test_secret_field_not_rendered_with_current_value(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        _seed_config(
            tmp_path,
            {
                "schema_version": "0.1",
                "northbound": {"mqtt": {"host": "127.0.0.1", "password": "super-secret-value"}},
            },
        )
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.get("/ui/config/core/northbound")
        assert response.status_code == 200
        assert "super-secret-value" not in response.text

    def test_blank_secret_field_on_save_preserves_prior_value(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        _seed_config(
            tmp_path,
            {
                "schema_version": "0.1",
                "northbound": {"mqtt": {"host": "127.0.0.1", "password": "super-secret-value"}},
            },
        )
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        # Change only the host; leave the password field blank.
        response = client.post(
            "/ui/config/core/northbound",
            data={"mqtt.host": "10.0.0.5", "publish_interval_seconds": "1.0"},
        )
        assert response.status_code == 200
        saved = _current_config(tmp_path)["northbound"]["mqtt"]
        assert saved["host"] == "10.0.0.5"
        assert saved["password"] == "super-secret-value"

    def test_new_secret_value_on_save_replaces_prior_value(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        _seed_config(
            tmp_path,
            {
                "schema_version": "0.1",
                "northbound": {"mqtt": {"host": "127.0.0.1", "password": "old-secret"}},
            },
        )
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.post(
            "/ui/config/core/northbound",
            data={
                "mqtt.host": "127.0.0.1",
                "mqtt.password": "${SECRET:new_ref}",
                "publish_interval_seconds": "1.0",
            },
        )
        assert response.status_code == 200
        assert _current_config(tmp_path)["northbound"]["mqtt"]["password"] == "${SECRET:new_ref}"

    def test_invalid_value_shows_error_and_does_not_write(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        original = (tmp_path / "xedge.yaml").read_text(encoding="utf-8")
        # watchdog.interval_seconds maximum is 30
        response = client.post(
            "/ui/config/core/watchdog", data={"enabled": "on", "interval_seconds": "999"}
        )
        assert response.status_code == 200
        assert "error" in response.text.lower()
        assert (tmp_path / "xedge.yaml").read_text(encoding="utf-8") == original


class TestDriverCrud:
    def test_add_driver_form_lists_known_types(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.get("/ui/config/drivers/new")
        assert response.status_code == 200
        assert "modbus_tcp" in response.text
        assert "opcua_client" in response.text

    def test_create_driver_then_redirects_to_edit_page(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.post(
            "/ui/config/drivers/new", data={"id": "modbus_tcp_01", "type": "modbus_tcp"}
        )
        assert response.status_code == 200
        assert response.url.path == "/ui/config/drivers/modbus_tcp_01"
        drivers = _current_config(tmp_path)["drivers"]
        assert drivers == [
            {
                "id": "modbus_tcp_01",
                "type": "modbus_tcp",
                "enabled": True,
                "config": {},
                "tag_groups": [],
            }
        ]

    def test_duplicate_driver_id_rejected(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post("/ui/config/drivers/new", data={"id": "d1", "type": "modbus_tcp"})
        response = client.post("/ui/config/drivers/new", data={"id": "d1", "type": "opcua_client"})
        assert response.status_code == 200
        assert "already exists" in response.text
        assert len(_current_config(tmp_path)["drivers"]) == 1

    def test_unknown_driver_type_rejected(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.post(
            "/ui/config/drivers/new", data={"id": "d1", "type": "not_a_real_type"}
        )
        assert response.status_code == 200
        assert "Unknown driver type" in response.text
        assert "drivers" not in _current_config(tmp_path) or not _current_config(tmp_path).get(
            "drivers"
        )

    def test_edit_driver_config_fields_and_save(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post("/ui/config/drivers/new", data={"id": "d1", "type": "modbus_tcp"})
        response = client.post(
            "/ui/config/drivers/d1",
            data={"enabled": "on", "host": "192.168.1.50", "port": "502", "unit_id": "1"},
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        entry = _current_config(tmp_path)["drivers"][0]
        assert entry["config"]["host"] == "192.168.1.50"

    def test_saving_driver_without_required_host_shows_validation_error(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post("/ui/config/drivers/new", data={"id": "d1", "type": "modbus_tcp"})
        response = client.post("/ui/config/drivers/d1", data={"enabled": "on"})
        assert response.status_code == 200
        assert "error" in response.text.lower()

    def test_delete_driver_removes_it(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post("/ui/config/drivers/new", data={"id": "d1", "type": "modbus_tcp"})
        response = client.post("/ui/config/drivers/d1/delete")
        assert response.status_code == 200
        assert response.url.path == "/ui/config"
        assert _current_config(tmp_path).get("drivers", []) == []

    def test_editing_unknown_driver_shows_error(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.get("/ui/config/drivers/does-not-exist")
        assert response.status_code == 200
        assert "No driver with id" in response.text


class TestTagGroupAndTagCrud:
    def _driver(self, client: TestClient, driver_type: str = "modbus_tcp") -> None:
        # _validate_driver_section validates the *whole* driver (config +
        # tag_groups) on every save, not just the node being edited — so a
        # tag_group/tag save fails until the driver's own required fields
        # (e.g. modbus_tcp's config.host) are set, same as it would at
        # driver-start time. Tests exercising tag groups/tags need a
        # complete, valid driver underneath them.
        client.post("/ui/config/drivers/new", data={"id": "d1", "type": driver_type})
        if driver_type == "modbus_tcp":
            client.post("/ui/config/drivers/d1", data={"enabled": "on", "host": "127.0.0.1"})

    def test_add_tag_group_then_redirects_to_edit(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        response = client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        assert response.status_code == 200
        assert response.url.path == "/ui/config/drivers/d1/tag-groups/g1"
        groups = _current_config(tmp_path)["drivers"][0]["tag_groups"]
        assert groups[0]["id"] == "g1"

    def test_duplicate_tag_group_id_rejected(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        response = client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        assert "already exists" in response.text

    def test_edit_tag_group_deadband_nested_object(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        client.post("/ui/config/drivers/d1/tag-groups/g1/tags/new", data={"id": "t1"})
        client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/t1",
            data={"function_code": "read_holding_registers", "address": "0"},
        )
        response = client.post(
            "/ui/config/drivers/d1/tag-groups/g1",
            data={
                "scan_rate_ms": "500",
                "deadband.type": "percentage",
                "deadband.value": "1.5",
            },
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        group = _current_config(tmp_path)["drivers"][0]["tag_groups"][0]
        assert group["deadband"] == {"type": "percentage", "value": 1.5}
        assert group["scan_rate_ms"] == 500

    def test_scan_rate_below_minimum_rejected(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        client.post("/ui/config/drivers/d1/tag-groups/g1/tags/new", data={"id": "t1"})
        client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/t1",
            data={"function_code": "read_holding_registers", "address": "0"},
        )
        # modbus_tcp tag_groups.scan_rate_ms minimum is 1 since XEDGE-413
        # lowered it from 50 (open item Q-2), so 0 is the rejected value.
        response = client.post("/ui/config/drivers/d1/tag-groups/g1", data={"scan_rate_ms": "0"})
        assert "error" in response.text.lower()

    def test_add_and_edit_tag_with_enum_and_scaling(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        response = client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/new", data={"id": "temperature_01"}
        )
        assert response.url.path == "/ui/config/drivers/d1/tag-groups/g1/tags/temperature_01"
        response = client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/temperature_01",
            data={
                "function_code": "read_holding_registers",
                "address": "10",
                "scaling.scale": "0.1",
                "scaling.offset": "-273.15",
                "engineering_unit": "degC",
            },
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        tag = _current_config(tmp_path)["drivers"][0]["tag_groups"][0]["tags"][0]
        assert tag == {
            "id": "temperature_01",
            "function_code": "read_holding_registers",
            "address": 10,
            "scaling": {"scale": 0.1, "offset": -273.15},
            "engineering_unit": "degC",
        }

    def test_opcua_driver_tag_uses_node_id_not_function_code(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client, driver_type="opcua_client")
        client.post("/ui/config/drivers/d1", data={"enabled": "on", "endpoint_url": "opc.tcp://x"})
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        client.post("/ui/config/drivers/d1/tag-groups/g1/tags/new", data={"id": "t1"})
        response = client.get("/ui/config/drivers/d1/tag-groups/g1/tags/t1")
        assert "node_id" in response.text or "Node ID" in response.text
        response = client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/t1", data={"node_id": "ns=2;i=1"}
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        tag = _current_config(tmp_path)["drivers"][0]["tag_groups"][0]["tags"][0]
        assert tag["node_id"] == "ns=2;i=1"

    def test_delete_tag_removes_it(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        client.post("/ui/config/drivers/d1/tag-groups/g1/tags/new", data={"id": "t1"})
        client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/t1",
            data={"function_code": "read_coils", "address": "0"},
        )
        response = client.post("/ui/config/drivers/d1/tag-groups/g1/tags/t1/delete")
        assert response.status_code == 200
        assert response.url.path == "/ui/config/drivers/d1/tag-groups/g1"
        group = _current_config(tmp_path)["drivers"][0]["tag_groups"][0]
        assert group["tags"] == []

    def test_delete_tag_group_removes_it(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        response = client.post("/ui/config/drivers/d1/tag-groups/g1/delete")
        assert response.status_code == 200
        assert response.url.path == "/ui/config/drivers/d1"
        assert _current_config(tmp_path)["drivers"][0]["tag_groups"] == []

    def test_export_csv_contains_existing_tag(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        client.post("/ui/config/drivers/d1/tag-groups/g1/tags/new", data={"id": "t1"})
        client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/t1",
            data={"function_code": "read_coils", "address": "5"},
        )
        response = client.get("/ui/config/drivers/d1/tag-groups/g1/tags/export.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        lines = response.text.strip().splitlines()
        assert lines[0].split(",")[:3] == ["id", "function_code", "address"]
        assert "t1,read_coils,5" in response.text

    def test_import_csv_adds_new_tags(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})

        csv_text = "id,function_code,address\nt1,read_coils,0\nt2,read_holding_registers,10\n"
        response = client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/import",
            files={"file": ("tags.csv", csv_text, "text/csv")},
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        tags = _current_config(tmp_path)["drivers"][0]["tag_groups"][0]["tags"]
        assert {t["id"] for t in tags} == {"t1", "t2"}

    def test_import_csv_upserts_existing_tag_by_id(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})
        client.post("/ui/config/drivers/d1/tag-groups/g1/tags/new", data={"id": "t1"})
        client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/t1",
            data={"function_code": "read_coils", "address": "0"},
        )

        csv_text = "id,function_code,address\nt1,read_holding_registers,99\n"
        client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/import",
            files={"file": ("tags.csv", csv_text, "text/csv")},
        )
        tags = _current_config(tmp_path)["drivers"][0]["tag_groups"][0]["tags"]
        assert len(tags) == 1
        assert tags[0]["function_code"] == "read_holding_registers"
        assert tags[0]["address"] == 99

    def test_import_json_adds_tags_with_nested_scaling(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})

        payload = json.dumps(
            [
                {
                    "id": "t1",
                    "function_code": "read_holding_registers",
                    "address": 10,
                    "scaling": {"scale": 0.1, "offset": -273.15},
                }
            ]
        )
        response = client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/import",
            files={"file": ("tags.json", payload, "application/json")},
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        tag = _current_config(tmp_path)["drivers"][0]["tag_groups"][0]["tags"][0]
        assert tag["scaling"] == {"scale": 0.1, "offset": -273.15}

    def test_import_invalid_csv_row_leaves_config_unchanged(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})

        csv_text = "id,function_code,address\n,read_coils,0\n"
        response = client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/import",
            files={"file": ("tags.csv", csv_text, "text/csv")},
        )
        assert "Row 2" in response.text
        assert _current_config(tmp_path)["drivers"][0]["tag_groups"][0]["tags"] == []

    def test_import_validation_failure_leaves_config_unchanged(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        self._driver(client)
        client.post("/ui/config/drivers/d1/tag-groups/new", data={"id": "g1"})

        # Parses fine (function_code is a plain string field to unflatten,
        # which doesn't check enum membership) but fails the modbus_tcp
        # driver schema's own enum validation at save time.
        csv_text = "id,function_code,address\nt1,bogus_code,0\n"
        response = client.post(
            "/ui/config/drivers/d1/tag-groups/g1/tags/import",
            files={"file": ("tags.csv", csv_text, "text/csv")},
        )
        assert response.status_code == 200
        assert "error" in response.text.lower()
        assert _current_config(tmp_path)["drivers"][0]["tag_groups"][0]["tags"] == []


class TestAdvancedYamlEditor:
    def test_advanced_page_shows_current_yaml(self, tmp_path: Path, core_schema_path: Path) -> None:
        _seed_config(tmp_path, {"schema_version": "0.1", "logging": {"level": "DEBUG"}})
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.get("/ui/config/advanced")
        assert response.status_code == 200
        assert "DEBUG" in response.text

    def test_advanced_submit_writes_and_shows_success(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.post(
            "/ui/config/advanced",
            data={"yaml_text": "schema_version: '0.1'\nlogging:\n  level: ERROR\n"},
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        assert _current_config(tmp_path)["logging"]["level"] == "ERROR"

    def test_advanced_invalid_yaml_shows_error(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.post(
            "/ui/config/advanced", data={"yaml_text": "schema_version: '0.1'\n  bad: [oops\n"}
        )
        assert "Invalid YAML" in response.text
