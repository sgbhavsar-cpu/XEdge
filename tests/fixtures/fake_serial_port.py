"""A fake RTU slave sitting on the far end of a real pseudo-terminal (pty)
pair (Sprint C3, XEDGE-430).

The point of using a *pty* rather than a plain `socket.socketpair()` is that
a pty's follower end is a genuine character device supporting the same
termios ioctls a real serial port does (baud rate, parity, stop bits, RTS/
DTR modem-control lines) — so `serial_asyncio.open_serial_connection` and
`ModbusRtuSerialDriver`/`SerialBusManager` open and configure it exactly as
they would `/dev/ttyUSB0`, with no test-only code path in the driver or bus
manager. A socketpair would fail the moment pyserial called `tcsetattr` on
it.

Before this fixture, `xedge/drivers/modbus/serial.py` had **no** automated
coverage requiring a real or virtual serial port (its own module docstring
said so) — this is finding F-8 from XEDGE-DR-001, and the reason ADR-011
calls this fixture a Sprint C3 *prerequisite*, not an afterthought: the
`SerialBusManager` (XEDGE-431) is not testable without it.

**POSIX only.** Built on the stdlib `pty`/`termios` modules, unavailable on
Windows — tests using this fixture are skipped there via
`requires_pty`/`pytest.mark.skipif`, consistent with this repo's CI running
on `ubuntu-latest`. This also means the fixture cannot be exercised on a
Windows development machine; it is validated by Linux CI, the same as any
other POSIX-only path already in this codebase (e.g. `xedge.core.main`'s
SIGTERM handling).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable

from tests.fixtures.fake_modbus_server import _ModbusDatastore, read_rtu_request_pdu
from xedge.drivers.modbus import rtu_codec

HAS_PTY = sys.platform != "win32"

if HAS_PTY:
    import pty


class _AsyncFd:
    """Minimal non-blocking async read/write wrapper around a raw file
    descriptor, using the running loop's `add_reader` rather than a
    background thread — a pty controller fd is a real selectable fd, so
    this needs nothing more exotic than any other asyncio transport."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        os.set_blocking(fd, False)
        self._buffer = bytearray()
        self._waiters: list[asyncio.Future[None]] = []
        self._closed = False
        asyncio.get_event_loop().add_reader(fd, self._on_readable)

    def _on_readable(self) -> None:
        try:
            chunk = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        self._buffer.extend(chunk)
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def read_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            if self._closed:
                raise ConnectionResetError("fd closed while waiting for data")
            waiter = asyncio.get_event_loop().create_future()
            self._waiters.append(waiter)
            await waiter
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    def write(self, data: bytes) -> None:
        os.write(self._fd, data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        asyncio.get_event_loop().remove_reader(self._fd)
        os.close(self._fd)
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(ConnectionResetError("fd closed"))
        self._waiters.clear()


class FakeSerialSlave(_ModbusDatastore):
    """A fake RTU slave on the follower end of a pty pair.

    `port_path` is a real tty path (e.g. `/dev/pts/7`) suitable for handing
    straight to `ModbusRtuSerialDriver`/`SerialBusManager`'s `port` config —
    exactly as an operator would configure a real `/dev/ttyUSB0`.
    """

    def __init__(self) -> None:
        super().__init__()
        self._controller_fd, self._follower_fd = pty.openpty()
        self.port_path = os.ttyname(self._follower_fd)
        self._controller = _AsyncFd(self._controller_fd)
        self._task: asyncio.Task[None] | None = None
        # (unit_id, request_pdu) per request received, in arrival order —
        # lets a multi-slave test assert *which* slave a request was
        # actually addressed to, the same evidence request_log gives the
        # TCP-based fixtures.
        self.received: list[tuple[int, bytes]] = []

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._serve())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._controller.close()
        with contextlib.suppress(OSError):
            os.close(self._follower_fd)

    async def _serve(self) -> None:
        with contextlib.suppress(ConnectionResetError):
            while True:
                unit_id, request_pdu = await read_rtu_request_pdu(self._read_exact)
                self.received.append((unit_id, request_pdu))
                response_pdu = self.handle_request(request_pdu)
                self._controller.write(rtu_codec.encode_rtu_frame(unit_id, response_pdu))

    def _read_exact(self, n: int) -> Awaitable[bytes]:
        return self._controller.read_exact(n)
