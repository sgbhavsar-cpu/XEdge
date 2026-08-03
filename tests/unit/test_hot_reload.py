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


def _serial_entry(instance_id: str, port: str, unit_id: int) -> dict:
    return {
        "id": instance_id,
        "type": "modbus_rtu_serial",
        "config": {"port": port, "unit_id": unit_id},
        "tag_groups": [
            {
                "id": "g1",
                "scan_rate_ms": 1000,
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

    async def test_disabled_driver_is_stopped_but_remembered_not_removed(
        self, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        """Sprint 25, XEDGE-186: `enabled: false` must be distinct from
        "absent from config" — the instance stops, but its entry is
        remembered (not forgotten) so a later re-enable is detected as a
        change, and the driver's state reads DISABLED, not just gone."""
        registry, supervisor = registry_and_supervisor
        current = await apply_driver_changes([_entry("d1")], {}, registry, supervisor)
        disabled_entry = {**_entry("d1"), "enabled": False}
        updated = await apply_driver_changes([disabled_entry], current, registry, supervisor)

        assert updated == {"d1": disabled_entry}
        assert supervisor.status("d1").state == DriverState.DISABLED

    async def test_removed_driver_entry_is_forgotten_unlike_disabled(
        self, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        current = await apply_driver_changes([_entry("d1")], {}, registry, supervisor)
        updated = await apply_driver_changes([], current, registry, supervisor)

        assert updated == {}
        assert supervisor.status("d1").state == DriverState.STOPPED

    async def test_re_enabling_a_disabled_driver_restarts_it(self, registry_and_supervisor) -> None:  # type: ignore[no-untyped-def]
        registry, supervisor = registry_and_supervisor
        current = await apply_driver_changes([_entry("d1")], {}, registry, supervisor)
        disabled_entry = {**_entry("d1"), "enabled": False}
        current = await apply_driver_changes([disabled_entry], current, registry, supervisor)
        assert supervisor.status("d1").state == DriverState.DISABLED

        updated = await apply_driver_changes([_entry("d1")], current, registry, supervisor)
        await asyncio.sleep(0.05)  # start() schedules the task; let it reach RUNNING

        assert updated == {"d1": _entry("d1")}
        assert supervisor.status("d1").state == DriverState.RUNNING

    async def test_already_disabled_driver_is_not_re_disabled_every_cycle(
        self, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        """Calling disable() again every poll cycle would keep resetting
        state_changed_at even though nothing actually changed — this test
        pins that the reconciler only acts on the enabled->disabled
        transition, not on every cycle a disabled entry is still present."""
        registry, supervisor = registry_and_supervisor
        current = await apply_driver_changes([_entry("d1")], {}, registry, supervisor)
        disabled_entry = {**_entry("d1"), "enabled": False}
        current = await apply_driver_changes([disabled_entry], current, registry, supervisor)
        state_changed_at = supervisor.status("d1").state_changed_at

        await apply_driver_changes([disabled_entry], current, registry, supervisor)

        assert supervisor.status("d1").state_changed_at == state_changed_at

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

    async def test_a_slave_id_conflict_starts_neither_instance(
        self, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        """XEDGE-433: two modbus_rtu_serial entries claiming the same
        (port, unit_id) must both be refused — the schema on either one in
        isolation has no way to see the other."""
        registry, supervisor = registry_and_supervisor
        registry.register("modbus_rtu_serial", lambda: FakeDriver(emit_interval_seconds=100.0))
        conflicting_a = _serial_entry("a", "/dev/ttyUSB0", 5)
        conflicting_b = _serial_entry("b", "/dev/ttyUSB0", 5)

        updated = await apply_driver_changes(
            [conflicting_a, conflicting_b], {}, registry, supervisor
        )

        assert "a" not in updated
        assert "b" not in updated
        with pytest.raises(KeyError):
            supervisor.status("a")
        with pytest.raises(KeyError):
            supervisor.status("b")
        await supervisor.stop_all()

    async def test_different_unit_ids_on_one_port_both_start(self, registry_and_supervisor) -> None:  # type: ignore[no-untyped-def]
        """The multi-drop case this whole feature exists to support must
        not be mistaken for a conflict."""
        registry, supervisor = registry_and_supervisor
        registry.register("modbus_rtu_serial", lambda: FakeDriver(emit_interval_seconds=100.0))
        entry_a = _serial_entry("a", "/dev/ttyUSB0", 1)
        entry_b = _serial_entry("b", "/dev/ttyUSB0", 2)

        updated = await apply_driver_changes([entry_a, entry_b], {}, registry, supervisor)

        assert {"a", "b"} <= updated.keys()
        assert supervisor.status("a") is not None
        assert supervisor.status("b") is not None
        await supervisor.stop_all()

    async def test_conflict_introduced_by_an_edit_reverts_to_prior_config(
        self, registry_and_supervisor
    ) -> None:  # type: ignore[no-untyped-def]
        """An instance running peacefully must not be torn down just
        because a *different* instance's edit now collides with it — the
        existing "prior config keeps running" rule (FR-CM-005) applies to
        a newly-introduced conflict exactly as it does to a schema
        failure."""
        registry, supervisor = registry_and_supervisor
        registry.register("modbus_rtu_serial", lambda: FakeDriver(emit_interval_seconds=100.0))
        original_a = _serial_entry("a", "/dev/ttyUSB0", 1)
        current = await apply_driver_changes([original_a], {}, registry, supervisor)

        edited_a = _serial_entry("a", "/dev/ttyUSB0", 5)  # now collides with b
        entry_b = _serial_entry("b", "/dev/ttyUSB0", 5)
        updated = await apply_driver_changes([edited_a, entry_b], current, registry, supervisor)

        assert updated["a"] == original_a, "must revert to the last-known-good entry"
        assert "b" not in updated
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
