"""systemd watchdog integration (NFR-R-005).

Sends `WATCHDOG=1` to the socket named by the `NOTIFY_SOCKET` environment
variable, which systemd sets when `WatchdogSec=` is configured on the unit.
Implemented directly over a Unix datagram socket to avoid an extra
dependency — this is the entire sd_notify wire protocol we need.
"""

from __future__ import annotations

import asyncio
import os
import socket

from xedge.observability.logging import get_logger

logger = get_logger(__name__)


def _notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address[0] == "@":
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message.encode("utf-8"))
    return True


async def watchdog_loop(interval_seconds: float, *, enabled: bool = True) -> None:
    """Kick the systemd watchdog every `interval_seconds` until cancelled.

    No-op (but still runs, logging once) when NOTIFY_SOCKET is unset — e.g.
    running outside systemd during development — or when disabled by config.
    """
    if not enabled:
        logger.info("watchdog.disabled")
        return

    notified_ready = _notify("READY=1")
    if not notified_ready:
        logger.info(
            "watchdog.no_notify_socket",
            detail="not running under systemd; watchdog kicks are no-ops",
        )

    try:
        while True:
            _notify("WATCHDOG=1")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        raise
