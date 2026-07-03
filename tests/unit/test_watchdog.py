from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from xedge.core.watchdog import watchdog_loop


async def test_watchdog_disabled_returns_immediately() -> None:
    await asyncio.wait_for(watchdog_loop(interval_seconds=10, enabled=False), timeout=1.0)


async def test_watchdog_no_notify_socket_env_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    task = asyncio.create_task(watchdog_loop(interval_seconds=0.01))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="AF_UNIX unavailable on this platform; xEdge targets Linux only (NFR-C-001)",
)
async def test_watchdog_sends_datagrams_to_notify_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(socket_path)
    server.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", socket_path)

    task = asyncio.create_task(watchdog_loop(interval_seconds=0.01))
    try:
        loop = asyncio.get_running_loop()
        received = await asyncio.wait_for(loop.sock_recv(server, 64), timeout=1.0)
        assert received == b"READY=1"
        received = await asyncio.wait_for(loop.sock_recv(server, 64), timeout=1.0)
        assert received == b"WATCHDOG=1"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.close()
