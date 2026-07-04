from __future__ import annotations

from pathlib import Path

import pytest

from xedge.core.config import (
    ConfigEngine,
    ConfigValidationError,
    ConfigVersionHistory,
    SecretResolutionError,
    SecretsResolver,
    deep_merge,
)


def test_deep_merge_overlay_wins_on_scalars() -> None:
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    overlay = {"a": 10, "b": {"c": 20}}
    assert deep_merge(base, overlay) == {"a": 10, "b": {"c": 20, "d": 3}}


def test_deep_merge_lists_replaced_not_concatenated() -> None:
    base = {"items": [1, 2, 3]}
    overlay = {"items": [4]}
    assert deep_merge(base, overlay) == {"items": [4]}


def test_deep_merge_does_not_mutate_inputs() -> None:
    base = {"a": {"b": 1}}
    overlay = {"a": {"c": 2}}
    result = deep_merge(base, overlay)
    result["a"]["b"] = 999
    assert base["a"]["b"] == 1


def test_secrets_resolver_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PASSWORD", "hunter2")
    resolver = SecretsResolver()
    assert resolver.resolve("${SECRET:my_password}") == "hunter2"


def test_secrets_resolver_from_file(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "api_key").write_text("s3cr3t\n", encoding="utf-8")
    resolver = SecretsResolver(secrets_dir=secrets_dir)
    assert resolver.resolve("${SECRET:api_key}") == "s3cr3t"


def test_secrets_resolver_unresolvable_raises() -> None:
    resolver = SecretsResolver()
    with pytest.raises(SecretResolutionError):
        resolver.resolve("${SECRET:does_not_exist}")


def test_secrets_resolver_recurses_into_nested_structures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "abc123")
    resolver = SecretsResolver()
    resolved = resolver.resolve({"a": ["${SECRET:token}", {"b": "${SECRET:token}"}]})
    assert resolved == {"a": ["abc123", {"b": "abc123"}]}


def test_config_engine_loads_valid_base(tmp_path: Path, core_schema_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("schema_version: '0.1'\nlogging:\n  level: DEBUG\n", encoding="utf-8")
    engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
    store = engine.load()
    assert store.get_section("logging") == {"level": "DEBUG"}


def test_config_engine_rejects_invalid_schema_version(
    tmp_path: Path, core_schema_path: Path
) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("schema_version: 'not-a-version'\n", encoding="utf-8")
    engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
    with pytest.raises(ConfigValidationError):
        engine.load()


def test_config_engine_layers_environment_overlay(tmp_path: Path, core_schema_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("schema_version: '0.1'\nlogging:\n  level: INFO\n", encoding="utf-8")
    env_overlay = tmp_path / "env.yaml"
    env_overlay.write_text("logging:\n  level: DEBUG\n", encoding="utf-8")

    engine = ConfigEngine(
        base_path=base, schema_path=core_schema_path, environment_path=env_overlay
    )
    store = engine.load()
    assert store.get_section("logging") == {"level": "DEBUG"}


def test_config_store_notifies_subscribers_on_replace(
    tmp_path: Path, core_schema_path: Path
) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("schema_version: '0.1'\n", encoding="utf-8")
    engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
    store = engine.load()

    notifications = []
    store.subscribe(lambda s: notifications.append(s.get_section("schema_version")))

    base.write_text("schema_version: '0.2'\n", encoding="utf-8")
    engine.reload(store)

    assert notifications == ["0.2"]


def test_config_store_reload_rejects_invalid_and_keeps_old(
    tmp_path: Path, core_schema_path: Path
) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("schema_version: '0.1'\n", encoding="utf-8")
    engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
    store = engine.load()

    base.write_text("schema_version: 'oops'\n", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        engine.reload(store)

    assert store.get_section("schema_version") == "0.1"


class TestConfigVersionHistory:
    def test_save_assigns_incrementing_version_ids(self, tmp_path: Path) -> None:
        history = ConfigVersionHistory(tmp_path / "history")
        v1 = history.save({"a": 1})
        v2 = history.save({"a": 2})
        assert v1 == 1
        assert v2 == 2
        assert history.list_versions() == [1, 2]

    def test_load_version_returns_saved_content(self, tmp_path: Path) -> None:
        history = ConfigVersionHistory(tmp_path / "history")
        version_id = history.save({"schema_version": "0.1", "drivers": []})
        assert history.load_version(version_id) == {"schema_version": "0.1", "drivers": []}

    def test_load_version_unknown_raises(self, tmp_path: Path) -> None:
        history = ConfigVersionHistory(tmp_path / "history")
        with pytest.raises(FileNotFoundError):
            history.load_version(999)

    def test_prunes_oldest_beyond_max_versions(self, tmp_path: Path) -> None:
        history = ConfigVersionHistory(tmp_path / "history", max_versions=3)
        for i in range(5):
            history.save({"n": i})
        assert history.list_versions() == [3, 4, 5]
        assert history.load_version(3) == {"n": 2}

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        history1 = ConfigVersionHistory(tmp_path / "history")
        history1.save({"a": 1})
        history2 = ConfigVersionHistory(tmp_path / "history")
        assert history2.list_versions() == [1]


class TestConfigEngineVersionHistoryAndRollback:
    def test_load_saves_a_version(self, tmp_path: Path, core_schema_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("schema_version: '0.1'\n", encoding="utf-8")
        history = ConfigVersionHistory(tmp_path / "history")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path, version_history=history)
        engine.load()
        assert history.list_versions() == [1]

    def test_reload_saves_a_new_version(self, tmp_path: Path, core_schema_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("schema_version: '0.1'\n", encoding="utf-8")
        history = ConfigVersionHistory(tmp_path / "history")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path, version_history=history)
        store = engine.load()

        base.write_text("schema_version: '0.2'\n", encoding="utf-8")
        engine.reload(store)
        assert history.list_versions() == [1, 2]

    def test_secrets_are_not_persisted_to_version_history(
        self, tmp_path: Path, core_schema_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_SECRET", "hunter2")
        base = tmp_path / "base.yaml"
        base.write_text(
            "schema_version: '0.1'\ndrivers:\n"
            "  - id: d1\n    type: modbus_tcp\n    config:\n"
            "      password: '${SECRET:my_secret}'\n",
            encoding="utf-8",
        )
        history = ConfigVersionHistory(tmp_path / "history")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path, version_history=history)
        store = engine.load()

        # the live store has the resolved secret...
        assert store.get_section("drivers")[0]["config"]["password"] == "hunter2"
        # ...but the on-disk history keeps the placeholder, not the plaintext.
        saved = history.load_version(1)
        assert saved["drivers"][0]["config"]["password"] == "${SECRET:my_secret}"

    def test_rollback_restores_prior_version(self, tmp_path: Path, core_schema_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("schema_version: '0.1'\n", encoding="utf-8")
        history = ConfigVersionHistory(tmp_path / "history")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path, version_history=history)
        store = engine.load()  # version 1: schema_version 0.1

        base.write_text("schema_version: '0.2'\n", encoding="utf-8")
        engine.reload(store)  # version 2: schema_version 0.2
        assert store.get_section("schema_version") == "0.2"

        engine.rollback(store, version_id=1)
        assert store.get_section("schema_version") == "0.1"
        # rollback itself becomes a new version
        assert history.list_versions() == [1, 2, 3]
        assert history.load_version(3) == {"schema_version": "0.1"}

    def test_rollback_without_history_configured_raises(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("schema_version: '0.1'\n", encoding="utf-8")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
        store = engine.load()
        with pytest.raises(ConfigValidationError, match="No version history"):
            engine.rollback(store, version_id=1)

    def test_rollback_to_invalid_version_raises_and_keeps_current(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("schema_version: '0.1'\n", encoding="utf-8")
        history = ConfigVersionHistory(tmp_path / "history")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path, version_history=history)
        store = engine.load()
        with pytest.raises(FileNotFoundError):
            engine.rollback(store, version_id=999)
        assert store.get_section("schema_version") == "0.1"

    def test_attach_version_history_saves_current_config_immediately(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("schema_version: '0.1'\n", encoding="utf-8")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
        engine.load()  # bootstrap load, no history attached yet

        history = ConfigVersionHistory(tmp_path / "history")
        engine.attach_version_history(history)
        assert history.list_versions() == [1]
        assert history.load_version(1) == {"schema_version": "0.1"}

    def test_attach_version_history_without_save_current_does_not_save(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("schema_version: '0.1'\n", encoding="utf-8")
        engine = ConfigEngine(base_path=base, schema_path=core_schema_path)
        store = engine.load()  # bootstrap load, no history attached yet

        history = ConfigVersionHistory(tmp_path / "history")
        engine.attach_version_history(history, save_current=False)
        assert history.list_versions() == []

        # subsequent reload uses the newly attached history
        base.write_text("schema_version: '0.2'\n", encoding="utf-8")
        engine.reload(store)
        assert history.list_versions() == [1]
