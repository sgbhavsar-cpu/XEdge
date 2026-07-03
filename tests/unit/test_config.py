from __future__ import annotations

from pathlib import Path

import pytest

from xedge.core.config import (
    ConfigEngine,
    ConfigValidationError,
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
