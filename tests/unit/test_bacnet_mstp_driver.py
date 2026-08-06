"""BacnetMstpDriver against a mocked subprocess + Unix-socket boundary.

Unlike the BACnet/IP driver (tested against a real local device,
tests/integration/test_bacnet_client_driver.py) or the MS/TP daemon
itself (verified against a real bacnet-stack server over a real
socat-bridged serial pair during Sprint P7 PR development -- see
xedge/drivers/bacnet/mstp_daemon/README.md), this driver's own unit
tests mock the daemon subprocess and its Unix domain socket. The daemon
process and the wire protocol it speaks are already covered by a real,
non-mocked end-to-end check; what these tests cover is this driver
class's own logic -- lifecycle state handling, timeout/error paths, and
value coercion -- in isolation and without needing a compiled C binary
or a virtual serial pair in CI.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from xedge.drivers.bacnet import mstp_client as mstp_client_module
from xedge.drivers.bacnet.mstp_client import BacnetMstpDriver, _coerce_value
from xedge.drivers.base import DriverConfig, DriverConnectionError, Quality


class FakeStdout:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeProcess:
    """Stand-in for asyncio.subprocess.Process's public surface."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.stdout = FakeStdout()
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


class FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class FakeReader:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)

    async def readline(self) -> bytes:
        if not self._responses:
            return b""
        return self._responses.pop(0)


def _make_config(**overrides: object) -> DriverConfig:
    config = {
        "port": "/dev/ttyUSB0",
        "mac_address": 3,
        "device_instance": 4194302,
        **overrides,
    }
    return DriverConfig(
        instance_id="mstp_1", driver_type="bacnet_mstp", config=config, tag_groups=[]
    )


@pytest.fixture
def socket_always_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "exists", lambda self: True)


async def _connected_driver(
    monkeypatch: pytest.MonkeyPatch, responses: list[bytes]
) -> tuple[BacnetMstpDriver, FakeProcess, FakeWriter]:
    monkeypatch.setattr(Path, "exists", lambda self: True)
    process = FakeProcess()
    writer = FakeWriter()
    reader = FakeReader(responses)
    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        patch("asyncio.open_unix_connection", new=AsyncMock(return_value=(reader, writer))),
    ):
        driver = BacnetMstpDriver()
        await driver.configure(_make_config())
        await driver.connect()
    return driver, process, writer


class TestConnect:
    async def test_success(self, socket_always_exists: None) -> None:
        process = FakeProcess()
        reader, writer = FakeReader([]), FakeWriter()
        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
            patch("asyncio.open_unix_connection", new=AsyncMock(return_value=(reader, writer))),
        ):
            driver = BacnetMstpDriver()
            await driver.configure(_make_config())
            await driver.connect()
        assert driver._process is process
        assert driver._writer is writer

    async def test_daemon_binary_not_found(self, socket_always_exists: None) -> None:
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("no such file")),
        ):
            driver = BacnetMstpDriver()
            await driver.configure(_make_config())
            with pytest.raises(DriverConnectionError, match="failed to start"):
                await driver.connect()

    async def test_daemon_exits_during_startup(self) -> None:
        process = FakeProcess()
        process.returncode = 1
        process.stdout = FakeStdout(b"fatal: could not open serial port\n")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            driver = BacnetMstpDriver()
            await driver.configure(_make_config())
            with pytest.raises(DriverConnectionError, match="exited during startup"):
                await driver.connect()

    async def test_socket_never_appears_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mstp_client_module, "_SOCKET_READY_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        process = FakeProcess()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            driver = BacnetMstpDriver()
            await driver.configure(_make_config())
            with pytest.raises(DriverConnectionError, match="did not create its socket"):
                await driver.connect()
        assert process.terminated


class TestReadTag:
    async def test_numeric_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver, _, writer = await _connected_driver(
            monkeypatch, [json.dumps({"ok": True, "value": 72.5}).encode() + b"\n"]
        )
        tag = {
            "id": "t1",
            "device_instance": 260001,
            "mac_address": 1,
            "object_type": "analog-input",
            "object_instance": 1,
            "property_id": "present-value",
        }
        update = await driver._read_tag("mstp_1", tag, 5.0)
        assert update.value == 72.5
        assert update.quality == Quality.GOOD
        sent = json.loads(writer.written[0].decode())
        assert sent == {
            "device_instance": 260001,
            "mac": 1,
            "object_type": "analog-input",
            "object_instance": 1,
            "property_id": "present-value",
        }

    async def test_binary_present_value_coerces_to_bool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver, _, _ = await _connected_driver(
            monkeypatch, [json.dumps({"ok": True, "value": 1}).encode() + b"\n"]
        )
        tag = {
            "id": "t1",
            "device_instance": 260001,
            "mac_address": 1,
            "object_type": "binary-input",
            "object_instance": 1,
            "property_id": "present-value",
        }
        update = await driver._read_tag("mstp_1", tag, 5.0)
        assert update.value is True
        assert update.quality == Quality.GOOD

    async def test_error_response_is_bad_quality(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver, _, _ = await _connected_driver(
            monkeypatch,
            [json.dumps({"ok": False, "error": "timeout"}).encode() + b"\n"],
        )
        tag = {
            "id": "t1",
            "device_instance": 260001,
            "mac_address": 1,
            "object_type": "analog-input",
            "object_instance": 1,
        }
        update = await driver._read_tag("mstp_1", tag, 5.0)
        assert update.quality == Quality.BAD
        assert update.metadata["bacnet_mstp_error"] == "timeout"

    async def test_daemon_disconnect_propagates_as_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead daemon breaks every subsequent read on this connection,
        not just one -- unlike a per-tag timeout/error, this deliberately
        is *not* caught into a Bad-quality TagUpdate. It propagates out of
        run() so DriverSupervisor treats it as a connection failure and
        reconnects (respawning a fresh daemon), the same as any other
        driver's connect()-time failure."""
        driver, _, _ = await _connected_driver(monkeypatch, [b""])
        tag = {
            "id": "t1",
            "device_instance": 260001,
            "mac_address": 1,
            "object_type": "analog-input",
            "object_instance": 1,
        }
        with pytest.raises(DriverConnectionError, match="closed the connection"):
            await driver._read_tag("mstp_1", tag, 5.0)


class TestDisconnect:
    async def test_terminates_process_and_closes_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver, process, writer = await _connected_driver(monkeypatch, [])
        await driver.disconnect()
        assert writer.closed
        assert process.terminated
        assert driver._process is None
        assert driver._writer is None

    async def test_kills_process_that_wont_terminate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver, process, _ = await _connected_driver(monkeypatch, [])
        monkeypatch.setattr(mstp_client_module, "_DAEMON_TERMINATE_TIMEOUT_SECONDS", 0.05)

        async def never_finishes() -> int:
            await asyncio.sleep(10)
            return 0

        process.wait = never_finishes  # type: ignore[method-assign]
        await driver.disconnect()
        assert process.killed


class TestCoerceValue:
    def test_binary_input_present_value_int_becomes_bool(self) -> None:
        assert _coerce_value(1, "binary-input", "present-value") is True
        assert _coerce_value(0, "binary-input", "present-value") is False

    def test_binary_input_other_property_not_coerced(self) -> None:
        assert _coerce_value(5, "binary-input", "priority-array") == 5

    def test_analog_value_passes_through(self) -> None:
        assert _coerce_value(72.5, "analog-input", "present-value") == 72.5

    def test_string_passes_through(self) -> None:
        assert _coerce_value("hello", "device", "object-name") == "hello"

    def test_default_property_id_assumed_present_value(self) -> None:
        assert _coerce_value(1, "binary-value", None) is True
