from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.mqtt_broker import mqtt_broker  # noqa: F401 — re-exported fixture
from tests.fixtures.opcua_server import opcua_test_server  # noqa: F401 — re-exported fixture
from xedge.drivers.base import TagUpdate

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_SCHEMA_PATH = REPO_ROOT / "config" / "schema" / "xedge-core.schema.json"


@pytest.fixture
def core_schema_path() -> Path:
    return CORE_SCHEMA_PATH


@pytest.fixture
def tag_queue() -> Iterator[asyncio.Queue[TagUpdate]]:
    yield asyncio.Queue(maxsize=1000)
