"""End-to-end fleet management test: a real Fleet Manager (uvicorn, real
HTTP, same shape as test_app_lifecycle.py's REST API test) and a real
`fleet_heartbeat_loop` agent talking to it — registration, heartbeat,
config push delivered on the next heartbeat, and the apply result reported
back on the one after that.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn

from xedge.core.config import ConfigValidator
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.fleet.agent import FleetAgentConfig, FleetAgentStatus, fleet_heartbeat_loop
from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.registry import DeviceRegistry

_JOIN_TOKEN = "test-join-token"
_ADMIN_TOKEN = "test-admin-token"
_HEARTBEAT_INTERVAL_SECONDS = 0.05


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until(predicate, timeout_seconds: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    pytest.fail("condition never became true within timeout")


async def test_agent_registers_heartbeats_and_applies_pushed_config(
    tmp_path: Path, core_schema_path: Path
) -> None:
    port = _free_port()
    registry = DeviceRegistry(tmp_path / "manager" / "devices.db")
    manager_app = create_fleet_manager_app(
        registry, join_token=_JOIN_TOKEN, admin_token=_ADMIN_TOKEN
    )
    manager_server = uvicorn.Server(
        uvicorn.Config(manager_app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    )
    manager_task = asyncio.create_task(manager_server.serve())

    manager_base_url = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                if (await client.get(f"{manager_base_url}/health", timeout=0.5)).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.05)
        else:
            pytest.fail("fleet manager never became reachable")

    config_path = tmp_path / "xedge.yaml"
    config_path.write_text("schema_version: '0.1'\n", encoding="utf-8")
    validator = ConfigValidator.from_file(core_schema_path)
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=10))
    fleet_status = FleetAgentStatus()
    fleet_config = FleetAgentConfig(
        manager_url=manager_base_url,
        device_id="test-device-1",
        join_token=_JOIN_TOKEN,
        display_name="Test Device",
        heartbeat_interval_seconds=_HEARTBEAT_INTERVAL_SECONDS,
    )
    token_path = tmp_path / "fleet" / "device_token"

    agent_task = asyncio.create_task(
        fleet_heartbeat_loop(
            fleet_config, token_path, supervisor, config_path, validator,
            datetime.now(UTC), fleet_status,
        )
    )

    try:
        await _wait_until(lambda: fleet_status.last_heartbeat_ok is True)
        assert fleet_status.registered is True
        assert token_path.is_file()

        async with httpx.AsyncClient(base_url=manager_base_url) as admin_client:
            await _wait_until(
                lambda: registry.get("test-device-1") is not None
                and registry.get("test-device-1").status == "online"
            )

            push = await admin_client.post(
                "/api/v1/fleet/devices/test-device-1/config",
                headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
                json={"config": {"schema_version": "0.1", "logging": {"level": "DEBUG"}}},
            )
            assert push.status_code == 202

            # Delivered on the agent's next heartbeat: it validates + writes
            # to the same file hot-reload watches (not applied here directly
            # — this test only proves the write, matching what
            # fleet_heartbeat_loop actually owns).
            await _wait_until(
                lambda: "level: DEBUG" in config_path.read_text(encoding="utf-8")
            )

            # Reported back on the heartbeat after that.
            await _wait_until(
                lambda: (registry.get("test-device-1").last_config_apply or {}).get("success")
                is True
            )
            record = registry.get("test-device-1")
            assert record.last_config_apply == {"version": 1, "success": True, "error": None}
            assert record.has_pending_config is False
    finally:
        agent_task.cancel()
        manager_server.should_exit = True
        await asyncio.gather(agent_task, manager_task, return_exceptions=True)
        registry.close()


async def test_agent_reports_a_rejected_config_without_writing_it(
    tmp_path: Path, core_schema_path: Path
) -> None:
    port = _free_port()
    registry = DeviceRegistry(tmp_path / "manager" / "devices.db")
    manager_app = create_fleet_manager_app(
        registry, join_token=_JOIN_TOKEN, admin_token=_ADMIN_TOKEN
    )
    manager_server = uvicorn.Server(
        uvicorn.Config(manager_app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    )
    manager_task = asyncio.create_task(manager_server.serve())
    manager_base_url = f"http://127.0.0.1:{port}"

    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                if (await client.get(f"{manager_base_url}/health", timeout=0.5)).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.05)
        else:
            pytest.fail("fleet manager never became reachable")

    config_path = tmp_path / "xedge.yaml"
    original_contents = "schema_version: '0.1'\n"
    config_path.write_text(original_contents, encoding="utf-8")
    validator = ConfigValidator.from_file(core_schema_path)
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=10))
    fleet_status = FleetAgentStatus()
    fleet_config = FleetAgentConfig(
        manager_url=manager_base_url,
        device_id="test-device-2",
        join_token=_JOIN_TOKEN,
        heartbeat_interval_seconds=_HEARTBEAT_INTERVAL_SECONDS,
    )
    token_path = tmp_path / "fleet" / "device_token"

    agent_task = asyncio.create_task(
        fleet_heartbeat_loop(
            fleet_config, token_path, supervisor, config_path, validator,
            datetime.now(UTC), fleet_status,
        )
    )
    try:
        await _wait_until(lambda: fleet_status.last_heartbeat_ok is True)

        async with httpx.AsyncClient(base_url=manager_base_url) as admin_client:
            # Missing required "schema_version" — invalid against the core schema.
            push = await admin_client.post(
                "/api/v1/fleet/devices/test-device-2/config",
                headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
                json={"config": {"logging": {"level": "DEBUG"}}},
            )
            assert push.status_code == 202

            await _wait_until(
                lambda: (registry.get("test-device-2").last_config_apply or {}).get("success")
                is False
            )
            record = registry.get("test-device-2")
            assert record.last_config_apply["error"]
            assert config_path.read_text(encoding="utf-8") == original_contents
    finally:
        agent_task.cancel()
        manager_server.should_exit = True
        await asyncio.gather(agent_task, manager_task, return_exceptions=True)
        registry.close()
