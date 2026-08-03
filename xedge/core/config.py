"""Config engine: load, merge, validate, and distribute configuration.

Implements FR-CM-001 (YAML + JSON Schema), FR-CM-002 (layered model),
FR-CM-005 (validate-or-reject), FR-CM-006 (version history / rollback),
FR-CM-007 (secrets substitution). The REST API (FR-CM-003) is later-sprint
work; this module provides the ConfigStore it will build on.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from xedge.core.assets import validate_asset_references

_SECRET_PATTERN = re.compile(r"\$\{SECRET:([A-Za-z0-9_.\-/]+)\}")

ChangeListener = Callable[["ConfigStore"], None]


class ConfigValidationError(Exception):
    """Raised when configuration fails JSON Schema validation.

    Carries a human-readable message (FR-CM-005 / FR-CM-008) rather than the
    raw jsonschema exception, so callers (CLI, REST API) can surface it
    directly to an operator.
    """


class SecretResolutionError(Exception):
    """Raised when a `${SECRET:name}` reference cannot be resolved."""


class ConfigValidator:
    """Wraps jsonschema.Draft7Validator with human-readable error formatting."""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        jsonschema.Draft7Validator.check_schema(schema)
        self._validator = jsonschema.Draft7Validator(schema)

    def validate(self, config: Mapping[str, Any]) -> None:
        errors = sorted(self._validator.iter_errors(config), key=lambda e: e.path)
        messages = [_format_error(e) for e in errors]
        # Asset referential integrity (XEDGE-461/ADR-010) is not expressible
        # in JSON Schema alone — it's a cross-section check (assets[]
        # against drivers[]) — so it runs here, after the schema check,
        # rather than as a second call every caller would have to remember
        # to make. `validate_asset_references` itself no-ops on a payload
        # with no "assets" key (every driver-type schema's own payload
        # shape), so this is safe to call unconditionally regardless of
        # which schema this particular ConfigValidator instance wraps.
        messages.extend(validate_asset_references(config))
        if not messages:
            return
        raise ConfigValidationError(
            f"Configuration failed validation ({len(messages)} error(s)):\n"
            + "\n".join(f"  - {m}" for m in messages)
        )

    @classmethod
    def from_file(cls, schema_path: Path) -> ConfigValidator:
        with schema_path.open("r", encoding="utf-8") as f:
            schema = yaml.safe_load(f) if schema_path.suffix in (".yaml", ".yml") else _load_json(f)
        return cls(schema)


def _load_json(f: Any) -> Any:
    return json.load(f)


def _format_error(error: jsonschema.exceptions.ValidationError) -> str:
    path = "/".join(str(p) for p in error.absolute_path) or "<root>"
    return f"{path}: {error.message}"


class SecretsResolver:
    """Resolves `${SECRET:name}` references (FR-CM-007).

    Backends tried in order: environment variable `name` (uppercased), then a
    file at `secrets_dir/name`. A Vault backend is a later-sprint addition.
    """

    def __init__(self, secrets_dir: Path | None = None) -> None:
        self._secrets_dir = secrets_dir

    def resolve(self, value: Any) -> Any:
        if isinstance(value, str):
            match = _SECRET_PATTERN.fullmatch(value)
            if match:
                return self._resolve_one(match.group(1))
            return _SECRET_PATTERN.sub(lambda m: str(self._resolve_one(m.group(1))), value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def _resolve_one(self, name: str) -> str:
        env_value = os.environ.get(name.upper())
        if env_value is not None:
            return env_value
        if self._secrets_dir is not None:
            secret_file = self._secrets_dir / name
            if secret_file.is_file():
                return secret_file.read_text(encoding="utf-8").strip()
        raise SecretResolutionError(
            f"Could not resolve ${{SECRET:{name}}} (checked env var and secrets dir)"
        )


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` onto `base`; overlay wins on conflicts.

    Dicts merge key-by-key; any other type (including lists) is replaced
    wholesale by the overlay's value.
    """
    result = dict(copy.deepcopy(base))
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = deep_merge(base_value, overlay_value)
        else:
            result[key] = copy.deepcopy(overlay_value)
    return result


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigValidationError(f"{path}: top-level YAML document must be a mapping")
    return data


class ConfigStore:
    """Holds the current, validated configuration and notifies subscribers.

    Components call `get_section(name)` for typed access to their own part of
    the config and `subscribe(listener)` to be notified after a successful
    reload (FR-CM-002 layered model; ConfigChanged event per system-architecture.md §3.1).
    """

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data: dict[str, Any] = dict(data)
        self._listeners: list[ChangeListener] = []

    @property
    def data(self) -> Mapping[str, Any]:
        return self._data

    def get_section(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)

    def subscribe(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    def replace(self, new_data: Mapping[str, Any]) -> None:
        """Swap in newly validated config and notify subscribers.

        Callers MUST validate `new_data` before calling this — ConfigStore
        itself does not validate (see ConfigEngine.load / .reload).
        """
        self._data = dict(new_data)
        for listener in list(self._listeners):
            listener(self)


class ConfigVersionHistory:
    """Persists the last `max_versions` config snapshots to disk for
    rollback (FR-CM-006), at `directory` (system-architecture.md §6.4:
    `/data/config-history/`).

    Stores the merged config **before** secrets substitution — never the
    resolved plaintext secrets — so `${SECRET:name}` placeholders remain in
    the on-disk history (SR-DS-001: sensitive values must not leak to disk
    in plaintext).
    """

    def __init__(self, directory: Path, max_versions: int = 10) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._max_versions = max_versions

    def _existing_version_ids(self) -> list[int]:
        ids = []
        for path in self._directory.glob("*.json"):
            try:
                ids.append(int(path.stem))
            except ValueError:
                continue
        return sorted(ids)

    def _version_path(self, version_id: int) -> Path:
        return self._directory / f"{version_id}.json"

    def save(self, config: Mapping[str, Any]) -> int:
        existing = self._existing_version_ids()
        version_id = (existing[-1] + 1) if existing else 1
        self._version_path(version_id).write_text(
            json.dumps(dict(config), indent=2), encoding="utf-8"
        )
        existing.append(version_id)
        while len(existing) > self._max_versions:
            self._version_path(existing.pop(0)).unlink(missing_ok=True)
        return version_id

    def list_versions(self) -> list[int]:
        return self._existing_version_ids()

    def load_version(self, version_id: int) -> dict[str, Any]:
        path = self._version_path(version_id)
        if not path.is_file():
            raise FileNotFoundError(f"No such config version: {version_id}")
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data


class ConfigEngine:
    """Loads, merges, validates, and produces a ConfigStore.

    Layering order (later overrides earlier), per system-architecture.md §3.1:
        base.yaml -> environment overlay (optional) -> runtime overrides (optional)
    """

    def __init__(
        self,
        base_path: Path,
        schema_path: Path,
        environment_path: Path | None = None,
        runtime_overrides_path: Path | None = None,
        secrets_resolver: SecretsResolver | None = None,
        version_history: ConfigVersionHistory | None = None,
    ) -> None:
        self._base_path = base_path
        self._environment_path = environment_path
        self._runtime_overrides_path = runtime_overrides_path
        self._validator = ConfigValidator.from_file(schema_path)
        self._secrets = secrets_resolver or SecretsResolver()
        self._version_history = version_history

    def attach_version_history(
        self, version_history: ConfigVersionHistory, *, save_current: bool = True
    ) -> None:
        """Attach version history after construction — useful when the
        history directory/retention settings themselves come from the
        config being loaded (bootstrapping: `load()` once without history,
        read those settings, then attach). `save_current` re-reads and
        records the on-disk config as a version immediately, so the config
        booted with is captured even though the initial `load()` predates
        this call."""
        self._version_history = version_history
        if save_current:
            merged = self._load_merged()
            self._validator.validate(merged)
            version_history.save(merged)

    def _load_merged(self) -> dict[str, Any]:
        merged = load_yaml(self._base_path)
        for overlay_path in (self._environment_path, self._runtime_overrides_path):
            if overlay_path is not None and overlay_path.is_file():
                merged = deep_merge(merged, load_yaml(overlay_path))
        return merged

    def load(self) -> ConfigStore:
        """Load, resolve secrets, validate, and return a new ConfigStore.

        Raises ConfigValidationError on invalid config (FR-CM-005) — the
        caller is responsible for refusing to start/reload in that case.
        """
        merged = self._load_merged()
        self._validator.validate(merged)
        if self._version_history is not None:
            self._version_history.save(merged)
        resolved = self._secrets.resolve(merged)
        return ConfigStore(resolved)

    def reload(self, store: ConfigStore) -> None:
        """Reload from disk into an existing ConfigStore, validating first.

        On validation failure, the existing store is left untouched and the
        error is re-raised (FR-CM-005: reject invalid config, keep running).
        """
        merged = self._load_merged()
        self._validator.validate(merged)
        if self._version_history is not None:
            self._version_history.save(merged)
        resolved = self._secrets.resolve(merged)
        store.replace(resolved)

    def rollback(self, store: ConfigStore, version_id: int) -> None:
        """Restore a prior config version (FR-CM-006). Re-validates before
        applying — a version that was valid when saved may not validate
        against a newer schema. The restored config is itself saved as a
        new version, so history reflects what actually ran, in order."""
        if self._version_history is None:
            raise ConfigValidationError("No version history configured; cannot roll back")
        merged = self._version_history.load_version(version_id)
        self._validator.validate(merged)
        self._version_history.save(merged)
        resolved = self._secrets.resolve(merged)
        store.replace(resolved)
