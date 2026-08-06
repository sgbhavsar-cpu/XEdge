"""A real bacnet-stack-backed MS/TP device (tests/fixtures/mstp_test_server/,
NOT the vendored third_party/bacnet-stack/apps/server-mini -- see that
directory's main.c docstring for why), bridged over a real socat virtual
serial pair -- the test double for BacnetMstpDriver's real-daemon
integration tests (tests/integration/test_bacnet_mstp_real_daemon.py).

Unlike tests/fixtures/bacnet_device.py (a pure-Python bacpypes3 object,
runs anywhere), this depends on two compiled Linux binaries and `socat`,
none of which exist on a plain `pip install -e ".[dev,test]"` checkout --
the daemon needs the third_party/bacnet-stack submodule fetched and a C
toolchain. `mstp_binaries_available()` is a module-level skip guard the
test module uses so `pytest tests/` (the main CI job, which deliberately
doesn't fetch the submodule) collects but skips these tests rather than
erroring; only the dedicated "BACnet MS/TP Daemon Build" CI job, which
builds both binaries first, actually runs them.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_SERVER_BIN = _REPO_ROOT / "tests" / "fixtures" / "mstp_test_server" / "mstp-test-server"
DAEMON_BIN = (
    _REPO_ROOT / "xedge" / "drivers" / "bacnet" / "mstp_daemon" / "xedge-bacnet-mstp-daemon"
)

# Must match tests/fixtures/mstp_test_server/main.c's TEST_ANALOG_VALUE /
# TEST_BINARY_ACTIVE constants exactly.
TEST_DEVICE_INSTANCE = 3000002
TEST_DEVICE_MAC = 1
TEST_ANALOG_VALUE = 85.3
TEST_BAUD_RATE = 38400

_SERVER_STARTUP_SECONDS = 1.0
_SOCAT_READY_TIMEOUT_SECONDS = 5.0


def mstp_binaries_available() -> bool:
    return TEST_SERVER_BIN.is_file() and DAEMON_BIN.is_file() and shutil.which("socat") is not None


@pytest.fixture
async def mstp_test_device() -> AsyncIterator[tuple[str, int, int]]:
    """Yields (master_side_port, test_device_mac, test_device_instance).
    `master_side_port` is the end of a real socat virtual serial pair a
    real BacnetMstpDriver (and the real xedge-bacnet-mstp-daemon binary it
    spawns) should connect through; the other end is owned by a real
    bacnet-stack-backed device (mstp_test_server) already running and
    already participating in MS/TP token-passing by the time this
    yields."""
    unique = uuid.uuid4().hex[:8]
    tmp_dir = Path(tempfile.gettempdir())
    device_side = tmp_dir / f"xedge-mstp-test-device-{unique}"
    master_side = tmp_dir / f"xedge-mstp-test-master-{unique}"

    socat = await asyncio.create_subprocess_exec(
        "socat",
        "-d",
        "-d",
        f"pty,raw,echo=0,link={device_side}",
        f"pty,raw,echo=0,link={master_side}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        deadline = asyncio.get_running_loop().time() + _SOCAT_READY_TIMEOUT_SECONDS
        while not (device_side.exists() and master_side.exists()):
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("socat did not create the virtual serial pair in time")
            await asyncio.sleep(0.05)

        server = await asyncio.create_subprocess_exec(
            str(TEST_SERVER_BIN),
            "--iface",
            str(device_side),
            "--mac",
            str(TEST_DEVICE_MAC),
            "--baud",
            str(TEST_BAUD_RATE),
            "--device-instance",
            str(TEST_DEVICE_INSTANCE),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # Let the test device bind its end and broadcast I-Am before
            # any driver-under-test tries to join the token-passing ring.
            await asyncio.sleep(_SERVER_STARTUP_SECONDS)
            yield str(master_side), TEST_DEVICE_MAC, TEST_DEVICE_INSTANCE
        finally:
            server.terminate()
            await server.wait()
    finally:
        socat.terminate()
        await socat.wait()
        device_side.unlink(missing_ok=True)
        master_side.unlink(missing_ok=True)
