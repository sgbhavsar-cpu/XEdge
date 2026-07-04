"""End-to-end smoke test: config load -> logging -> watchdog -> graceful shutdown.

Exercises the same wiring as `xedge --config ...` (XEDGE-004), without
depending on OS signal delivery — asyncio.Event is set directly in place of
waiting on SIGTERM, mirroring how the app behaves in production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from xedge.core import main as main_module

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MINIMAL_CONFIG = REPO_ROOT / "config" / "examples" / "modbus-minimal.yaml"
_TEST_API_PORT = 18765


async def test_async_main_starts_and_shuts_down_cleanly(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    async def immediate_shutdown() -> None:
        return None

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", immediate_shutdown)

    # Override the store directory so this doesn't touch the production
    # default (/data/store) on the machine running the tests, and disable
    # the REST API — its live behavior has its own dedicated test below,
    # and the default port can hit host-specific bind restrictions
    # (e.g. WinError 10013 on some Windows setups) unrelated to this test.
    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.read_text(encoding="utf-8")
        + f"\nstore:\n  directory: {tmp_path / 'store'}\n"
        + "\napi:\n  enabled: false\n",
        encoding="utf-8",
    )

    exit_code = await asyncio.wait_for(
        main_module.async_main(config_path, main_module._DEFAULT_SCHEMA_PATH),
        timeout=5.0,
    )
    assert exit_code == 0


async def test_async_main_serves_rest_api_over_real_http(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Live smoke test: actually binds uvicorn and makes real HTTP requests
    against it while async_main is running, rather than only exercising
    create_app() in-process (see tests/unit/test_api.py for that)."""
    shutdown_event = asyncio.Event()

    async def wait_for_test_shutdown() -> None:
        await shutdown_event.wait()

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", wait_for_test_shutdown)

    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.read_text(encoding="utf-8")
        + f"\nstore:\n  directory: {tmp_path / 'store'}\n"
        + f"\napi:\n  port: {_TEST_API_PORT}\n",
        encoding="utf-8",
    )

    task = asyncio.create_task(
        main_module.async_main(config_path, main_module._DEFAULT_SCHEMA_PATH)
    )
    try:
        base_url = f"http://127.0.0.1:{_TEST_API_PORT}"
        async with httpx.AsyncClient() as client:
            response = None
            for _ in range(100):
                try:
                    response = await client.get(f"{base_url}/health", timeout=0.5)
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("REST API never became reachable")
            assert response is not None
            assert response.json() == {"status": "ok"}

            status_response = await client.get(f"{base_url}/api/v1/status")
            assert status_response.status_code == 200
            assert "version" in status_response.json()

            drivers_response = await client.get(f"{base_url}/api/v1/drivers")
            assert drivers_response.status_code == 200

            config_response = await client.get(f"{base_url}/api/v1/config")
            assert config_response.status_code == 200
    finally:
        shutdown_event.set()
        exit_code = await asyncio.wait_for(task, timeout=5.0)
        assert exit_code == 0


async def test_async_main_survives_rest_api_bind_failure(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Regression test: uvicorn.Server.serve() calls sys.exit() internally
    on a bind failure (e.g. port already in use, or — as discovered during
    manual verification — a port already reserved system-wide by another
    service, like Windows IIS/http.sys on 8080), raising SystemExit — a
    BaseException — inside an unawaited asyncio.Task. Left unguarded, that
    crashed the entire xedge process (driver polling, northbound
    publishing, everything). The REST API failing to start must not take
    down the rest of the app.

    Synthesizes the failure directly (monkeypatching uvicorn.Server.serve)
    rather than relying on a real OS-level port conflict: what's under test
    is xedge's own exception-isolation code in _run_api_server, not uvicorn
    or OS/platform port-binding semantics, which vary too much across
    platforms (e.g. Windows doesn't reliably reject binds to low "privileged"
    ports the way POSIX does, and Windows SO_REUSEADDR semantics make a
    real same-process socket-conflict reproduction unreliable)."""
    import uvicorn

    def _fake_serve(self: uvicorn.Server) -> None:  # type: ignore[no-untyped-def]
        raise SystemExit(3)  # uvicorn.server.STARTUP_FAILURE

    monkeypatch.setattr(uvicorn.Server, "serve", _fake_serve)

    shutdown_event = asyncio.Event()

    async def wait_for_test_shutdown() -> None:
        await shutdown_event.wait()

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", wait_for_test_shutdown)

    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.read_text(encoding="utf-8")
        + f"\nstore:\n  directory: {tmp_path / 'store'}\n",
        encoding="utf-8",
    )

    task = asyncio.create_task(
        main_module.async_main(config_path, main_module._DEFAULT_SCHEMA_PATH)
    )
    try:
        # Give the (synthetic) API startup failure time to fire before
        # checking whether the app is still alive and well.
        await asyncio.sleep(0.5)
        assert not task.done(), (
            "async_main crashed instead of surviving the REST API startup failure "
            f"(exception: {task.exception() if task.done() else None})"
        )
    finally:
        shutdown_event.set()
        exit_code = await asyncio.wait_for(task, timeout=5.0)
        assert exit_code == 0
