from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.fixtures.fake_driver import FakeDriver
from xedge.core.config import ConfigEngine
from xedge.core.hot_reload import apply_driver_changes, config_watch_loop
from xedge.core.supervisor import DriverRegistry, DriverState, DriverSupervisor
from xedge.drivers.base import TagUpdate


def _entry(instance_id: str, scan_rate_ms: int = 1000) -> dict:
    return {
        "id": instance_id,
        "type": "modbus_tcp",
        "config": {"host": "127.0.0.1"},
        "tag_groups": [
            {
                "id": "g1",
                "scan_rate_ms": scan_rate_ms,
                "tags": [{"id": "t1", "function_code": "read_holding_registers", "address": 0}],
            }
        ],
    }


@pytest.fixture
def registry_and_supervisor():  # type: ignore[no-untyped-def]
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=100)
    registry = DriverRegistry()
    registry.register("modbus_tcp", lambda: FakeDriver(emit_interval_seconds=100.0))
    supervisor = DriverSupervisor(registry, queue)
    return registry, supervisor


class TestApplyDriverChanges:
    async def test_new_driver_is_started(self, registry_and_supervisor) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        current: dict = {}
        updated = await apply_driver_changes([_entry("d1")], current, registry, supervisor)
        assert "d1" in updated
        assert supervisor.status("d1") is not None
        await supervisor.stop_all()

    async def test_unchanged_driver_is_not_restarted(self, registry_and_supervisor) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        entry = _entry("d1")
        current = await apply_driver_changes([entry], {}, registry, supervisor)
        # applying the identical entry again must not attempt to re-start
        # (DriverSupervisor.start() raises if already running)
        await apply_driver_changes([entry], current, registry, supervisor)
        await supervisor.stop_all()

    async def test_changed_driver_is_restarted(self, registry_and_supervisor) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        current = await apply_driver_changes([_entry("d1", 1000)], {}, registry, supervisor)
        updated = await apply_driver_changes([_entry("d1", 500)], current, registry, supervisor)
        assert updated["d1"]["tag_groups"][0]["scan_rate_ms"] == 500
        assert supervisor.status("d1") is not None
        await supervisor.stop_all()

    async def test_removed_driver_is_stopped(self, registry_and_supervisor) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        current = await apply_driver_changes([_entry("d1")], {}, registry, supervisor)
        updated = await apply_driver_changes([], current, registry, supervisor)
        assert updated == {}
        assert supervisor.status("d1").state == DriverState.STOPPED

    async def test_disabled_driver_in_new_config_is_treated_as_removed(
        self, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        current = await apply_driver_changes([_entry("d1")], {}, registry, supervisor)
        disabled_entry = {**_entry("d1"), "enabled": False}
        updated = await apply_driver_changes([disabled_entry], current, registry, supervisor)
        assert updated == {}

    async def test_one_invalid_driver_entry_does_not_crash_or_block_others(
        self, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        # A stub tag group with no tags yet (as the Web UI's "add tag group"
        # flow briefly creates) fails modbus_tcp's tag_groups schema
        # (minItems: 1) — build_driver_config() raises ConfigValidationError.
        # This must not crash apply_driver_changes(): d1 is skipped and
        # logged, but d2 (unrelated and valid) still starts normally.
        registry, supervisor = registry_and_supervisor
        invalid_entry = {
            "id": "d1",
            "type": "modbus_tcp",
            "config": {"host": "127.0.0.1"},
            "tag_groups": [{"id": "g1", "scan_rate_ms": 1000, "tags": []}],
        }
        updated = await apply_driver_changes(
            [invalid_entry, _entry("d2")], {}, registry, supervisor
        )
        assert "d1" not in updated  # never started, so not tracked as current
        with pytest.raises(KeyError):
            supervisor.status("d1")
        assert "d2" in updated
        assert supervisor.status("d2") is not None
        await supervisor.stop_all()

    async def test_invalid_edit_of_a_running_driver_leaves_it_running_on_old_config(
        self, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        good_entry = _entry("d1", 1000)
        current = await apply_driver_changes([good_entry], {}, registry, supervisor)
        broken_entry = {
            **good_entry,
            "tag_groups": [{"id": "g1", "scan_rate_ms": 1000, "tags": []}],
        }
        updated = await apply_driver_changes([broken_entry], current, registry, supervisor)
        # d1 was left running (never stopped) on its last-known-good config,
        # and the tracked entry stays the old one so a later valid edit is
        # still recognized as "changed" and retried.
        assert supervisor.status("d1").state != DriverState.STOPPED
        assert updated["d1"] == good_entry
        await supervisor.stop_all()


class TestConfigWatchLoop:
    async def test_detects_file_change_and_reconciles_drivers(
        self, tmp_path: Path, core_schema_path: Path, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        base = tmp_path / "xedge.yaml"
        base.write_text("schema_version: '0.1'\ndrivers: []\n", encoding="utf-8")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
        store = engine.load()
        current: dict = {}

        task = asyncio.create_task(
            config_watch_loop(
                engine, store, base, registry, supervisor, current, poll_interval_seconds=0.01
            )
        )
        try:
            await asyncio.sleep(0.03)
            import yaml

            new_config = {"schema_version": "0.1", "drivers": [_entry("d1")]}
            base.write_text(yaml.safe_dump(new_config), encoding="utf-8")

            for _ in range(200):
                if "d1" in current:
                    break
                await asyncio.sleep(0.01)
            assert "d1" in current
            assert supervisor.status("d1") is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await supervisor.stop_all()

    async def test_invalid_reload_is_rejected_and_loop_keeps_running(
        self, tmp_path: Path, core_schema_path: Path, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        base = tmp_path / "xedge.yaml"
        base.write_text("schema_version: '0.1'\n", encoding="utf-8")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
        store = engine.load()
        current: dict = {}

        task = asyncio.create_task(
            config_watch_loop(
                engine, store, base, registry, supervisor, current, poll_interval_seconds=0.01
            )
        )
        try:
            await asyncio.sleep(0.03)
            base.write_text("schema_version: 'not-valid'\n", encoding="utf-8")
            await asyncio.sleep(0.1)
            # invalid change rejected: store keeps the old, valid value
            assert store.get_section("schema_version") == "0.1"

            # loop is still alive and picks up a subsequent valid change
            import yaml

            base.write_text(
                yaml.safe_dump({"schema_version": "0.1", "drivers": [_entry("d1")]}),
                encoding="utf-8",
            )
            for _ in range(200):
                if "d1" in current:
                    break
                await asyncio.sleep(0.01)
            assert "d1" in current
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await supervisor.stop_all()
