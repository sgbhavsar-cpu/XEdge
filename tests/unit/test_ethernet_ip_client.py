"""EtherNetIpDriver against a mocked `pycomm3.LogixDriver` boundary.

Every other protocol-client driver in this codebase
(tests/integration/test_opcua_client_driver.py,
tests/integration/test_bacnet_client_driver.py) is tested against a real
local server. This driver is not, and that is a deliberate, documented
departure from that precedent, not an oversight:

`cpppo`'s CIP simulator (`cpppo.server.enip`) was tried first. It accepts a
`pycomm3.LogixDriver` connection successfully, but every subsequent
read/write returns `Tag(..., error="Tag doesn't exist")` — cpppo implements
generic/simple CIP (its own docs describe its `-S` flag as a "simple
(non-routing) ... device, e.g. MicroLogix"), not the Logix symbol-table
upload (CIP Symbol Object 0x6B) that `pycomm3.LogixDriver` requires to
resolve a tag name to an address and type. This is an architectural
mismatch, not a configuration problem, and no other maintained,
permissively-licensed, Logix-compatible CIP simulator was found. See
xedge/drivers/ethernet_ip/client.py's module docstring for the same note
next to the code it documents.

`pycomm3` is therefore treated as bought, trusted, third-party
infrastructure (ADR-012 §1's framing) and tested at its own public
boundary — `LogixDriver.open`/`close`/`read`/`write` — with a small fake
that reproduces its real return-value contracts (a `Tag` NamedTuple with a
falsy `__bool__` on error; a single `Tag` for one requested tag, a `list`
for several; `CommError` for a broken connection vs.
`DataError`/`ResponseError`/`RequestError` for a request/response-level
problem) rather than a loose mock that would let a wrong assumption about
that contract pass silently.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pycomm3.exceptions import CommError, DataError, RequestError
from pycomm3.tag import Tag as PycommTag

from xedge.drivers.base import (
    DriverConfig,
    DriverConnectionError,
    Quality,
    TagUpdate,
)
from xedge.drivers.ethernet_ip.client import EtherNetIpDriver


class FakeLogixDriver:
    """Stand-in for pycomm3.LogixDriver's public surface — see module
    docstring above for why a real simulator could not be used instead."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.socket_timeout: float | None = None
        self.open_result: bool | Exception = True
        self.read_result: PycommTag | list[PycommTag] | Exception = PycommTag(
            "unset", None, None, "read_result not configured by test"
        )
        self.write_result: PycommTag | Exception = PycommTag("unset", None, None, None)
        self.read_calls: list[tuple[str, ...]] = []
        self.write_calls: list[tuple[str, Any]] = []
        self.closed = False

    def bind(self, path: str) -> FakeLogixDriver:
        self.path = path
        return self

    def open(self) -> bool:
        if isinstance(self.open_result, Exception):
            raise self.open_result
        return self.open_result

    def close(self) -> None:
        self.closed = True

    def read(self, *tags: str) -> PycommTag | list[PycommTag]:
        self.read_calls.append(tags)
        if isinstance(self.read_result, Exception):
            raise self.read_result
        return self.read_result

    def write(self, tag: str, value: Any) -> PycommTag:
        self.write_calls.append((tag, value))
        if isinstance(self.write_result, Exception):
            raise self.write_result
        return self.write_result


@pytest.fixture
def fake_logix(monkeypatch: pytest.MonkeyPatch) -> FakeLogixDriver:
    fake = FakeLogixDriver()
    monkeypatch.setattr(
        "xedge.drivers.ethernet_ip.client.LogixDriver", lambda path: fake.bind(path)
    )
    return fake


def _driver_config(
    tags: list[dict[str, Any]], scan_rate_ms: int = 50, **config_overrides: Any
) -> DriverConfig:
    return DriverConfig(
        instance_id="enip_01",
        driver_type="ethernet_ip",
        config={"host": "10.0.0.5", "port": 44818, "slot": 0, **config_overrides},
        tag_groups=[{"id": "group1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
    )


async def _run_one_cycle(
    driver: EtherNetIpDriver, config: DriverConfig, expected_updates: int | None = None
) -> list[TagUpdate]:
    if expected_updates is None:
        expected_updates = len(config.tag_groups[0]["tags"])
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        return [await asyncio.wait_for(queue.get(), timeout=3.0) for _ in range(expected_updates)]
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_connect_builds_cip_path_from_host_port_slot(fake_logix: FakeLogixDriver) -> None:
    driver = EtherNetIpDriver()
    config = _driver_config([], host="10.0.0.5", port=44818, slot=2)
    await driver.configure(config)
    await driver.connect()

    assert fake_logix.path == "10.0.0.5:44818/2"
    await driver.disconnect()


async def test_connect_defaults_port_and_slot_when_omitted(fake_logix: FakeLogixDriver) -> None:
    driver = EtherNetIpDriver()
    config = DriverConfig(
        instance_id="enip_01",
        driver_type="ethernet_ip",
        config={"host": "10.0.0.9"},
        tag_groups=[],
    )
    await driver.configure(config)
    await driver.connect()

    assert fake_logix.path == "10.0.0.9:44818/0"
    await driver.disconnect()


async def test_connect_applies_configured_timeout_to_socket_timeout(
    fake_logix: FakeLogixDriver,
) -> None:
    driver = EtherNetIpDriver()
    config = _driver_config([], connect_timeout_seconds=2.5)
    await driver.configure(config)
    await driver.connect()

    assert fake_logix.socket_timeout == 2.5
    await driver.disconnect()


async def test_connect_raises_driver_connection_error_when_open_returns_false(
    fake_logix: FakeLogixDriver,
) -> None:
    fake_logix.open_result = False
    driver = EtherNetIpDriver()
    await driver.configure(_driver_config([]))

    with pytest.raises(DriverConnectionError):
        await driver.connect()


async def test_connect_raises_driver_connection_error_when_open_raises_commerror(
    fake_logix: FakeLogixDriver,
) -> None:
    fake_logix.open_result = CommError("no route to host")
    driver = EtherNetIpDriver()
    await driver.configure(_driver_config([]))

    with pytest.raises(DriverConnectionError):
        await driver.connect()


async def test_disconnect_closes_the_session(fake_logix: FakeLogixDriver) -> None:
    driver = EtherNetIpDriver()
    await driver.configure(_driver_config([]))
    await driver.connect()
    await driver.disconnect()

    assert fake_logix.closed is True


async def test_disconnect_before_connect_is_safe() -> None:
    driver = EtherNetIpDriver()
    await driver.disconnect()  # must not raise


async def test_read_single_tag_produces_good_quality_update(fake_logix: FakeLogixDriver) -> None:
    fake_logix.read_result = PycommTag("Speed", 42.5, "REAL", None)
    config = _driver_config([{"id": "speed", "tag_name": "Speed"}])
    driver = EtherNetIpDriver()

    updates = await _run_one_cycle(driver, config)

    assert updates[0].tag_id == "enip_01/speed"
    assert updates[0].value == 42.5
    assert updates[0].quality == Quality.GOOD
    assert updates[0].source_address == "Speed"
    assert updates[0].metadata == {"ethernet_ip_type": "REAL"}
    assert fake_logix.read_calls[0] == ("Speed",)


async def test_read_multiple_tags_matches_each_result_to_its_tag(
    fake_logix: FakeLogixDriver,
) -> None:
    fake_logix.read_result = [
        PycommTag("Running", True, "BOOL", None),
        PycommTag("FaultCode", None, None, "Tag doesn't exist"),
    ]
    config = _driver_config(
        [
            {"id": "running", "tag_name": "Running"},
            {"id": "fault", "tag_name": "FaultCode"},
        ]
    )
    driver = EtherNetIpDriver()

    updates = await _run_one_cycle(driver, config)

    by_id = {u.tag_id: u for u in updates}
    assert by_id["enip_01/running"].value is True
    assert by_id["enip_01/running"].quality == Quality.GOOD
    assert by_id["enip_01/fault"].quality == Quality.BAD
    assert by_id["enip_01/fault"].metadata == {"ethernet_ip_error": "Tag doesn't exist"}


async def test_tag_level_error_does_not_propagate_or_stop_polling(
    fake_logix: FakeLogixDriver,
) -> None:
    # A per-tag error comes back from pycomm3 as a Tag with .error set, not
    # as a raised exception (confirmed by reading pycomm3's own read()) —
    # it must be handled as Bad quality, never let escape run().
    fake_logix.read_result = PycommTag("Missing", None, None, "Tag doesn't exist")
    config = _driver_config([{"id": "missing", "tag_name": "Missing"}])
    driver = EtherNetIpDriver()

    updates = await _run_one_cycle(driver, config)

    assert updates[0].quality == Quality.BAD
    assert updates[0].value == 0


async def test_commerror_during_read_propagates_out_of_run(fake_logix: FakeLogixDriver) -> None:
    fake_logix.read_result = CommError("connection reset")
    config = _driver_config([{"id": "t1", "tag_name": "Tag1"}])
    driver = EtherNetIpDriver()
    await driver.configure(config)
    await driver.connect()

    with pytest.raises(CommError):
        await driver.run(asyncio.Queue())

    await driver.disconnect()


async def test_non_transport_pycommerror_marks_group_bad_without_propagating(
    fake_logix: FakeLogixDriver,
) -> None:
    fake_logix.read_result = RequestError("malformed tag request")
    config = _driver_config([{"id": "t1", "tag_name": "Tag1"}, {"id": "t2", "tag_name": "Tag2"}])
    driver = EtherNetIpDriver()

    updates = await _run_one_cycle(driver, config)

    assert all(u.quality == Quality.BAD for u in updates)
    assert {u.tag_id for u in updates} == {"enip_01/t1", "enip_01/t2"}


async def test_write_only_tags_are_excluded_from_polling(fake_logix: FakeLogixDriver) -> None:
    fake_logix.read_result = PycommTag("Speed", 1.0, "REAL", None)
    config = _driver_config(
        [
            {"id": "speed", "tag_name": "Speed"},
            {"id": "start_cmd", "tag_name": "StartCmd", "access": "write_only"},
        ]
    )
    driver = EtherNetIpDriver()

    updates = await _run_one_cycle(driver, config, expected_updates=1)

    assert updates[0].tag_id == "enip_01/speed"
    assert fake_logix.read_calls[0] == ("Speed",)


async def test_write_success(fake_logix: FakeLogixDriver) -> None:
    fake_logix.write_result = PycommTag("Setpoint", 72.0, "REAL", None)
    config = _driver_config([{"id": "setpoint", "tag_name": "Setpoint"}])
    driver = EtherNetIpDriver()
    await driver.configure(config)
    await driver.connect()

    result = await driver.write("setpoint", 72.0)

    assert result.success is True
    assert fake_logix.write_calls == [("Setpoint", 72.0)]
    await driver.disconnect()


async def test_write_applies_inverse_scaling(fake_logix: FakeLogixDriver) -> None:
    fake_logix.write_result = PycommTag("RawSpeed", 100, "DINT", None)
    config = _driver_config(
        [
            {
                "id": "speed",
                "tag_name": "RawSpeed",
                "scaling": {"scale": 0.1, "offset": 5.0},
            }
        ]
    )
    driver = EtherNetIpDriver()
    await driver.configure(config)
    await driver.connect()

    # engineering_value = raw * scale + offset  =>  raw = (engineering - offset) / scale
    await driver.write("speed", 15.0)

    assert fake_logix.write_calls == [("RawSpeed", 100.0)]
    await driver.disconnect()


async def test_write_without_scaling_preserves_value_type(fake_logix: FakeLogixDriver) -> None:
    # A tag with no scaling configured must not be coerced to float — a
    # BOOL/STRING tag would be corrupted by float(value) the way
    # xedge.drivers.modbus.polling's _inverse_scale does for its
    # always-numeric Modbus registers.
    fake_logix.write_result = PycommTag("Running", True, "BOOL", None)
    config = _driver_config([{"id": "running", "tag_name": "Running"}])
    driver = EtherNetIpDriver()
    await driver.configure(config)
    await driver.connect()

    await driver.write("running", True)

    assert fake_logix.write_calls == [("Running", True)]
    await driver.disconnect()


async def test_write_rejected_for_read_only_tag_without_touching_driver(
    fake_logix: FakeLogixDriver,
) -> None:
    config = _driver_config([{"id": "status", "tag_name": "Status", "access": "read_only"}])
    driver = EtherNetIpDriver()
    await driver.configure(config)
    await driver.connect()

    result = await driver.write("status", 1)

    assert result.success is False
    assert "read_only" in (result.error_message or "")
    assert fake_logix.write_calls == []
    await driver.disconnect()


async def test_write_unknown_tag_returns_failure_without_connecting(
    fake_logix: FakeLogixDriver,
) -> None:
    driver = EtherNetIpDriver()
    await driver.configure(_driver_config([{"id": "speed", "tag_name": "Speed"}]))

    result = await driver.write("does_not_exist", 1)

    assert result.success is False
    assert result.error_message == "Unknown tag"


async def test_write_tag_level_error_returns_failure_without_propagating(
    fake_logix: FakeLogixDriver,
) -> None:
    fake_logix.write_result = DataError("value out of range for tag type")
    config = _driver_config([{"id": "setpoint", "tag_name": "Setpoint"}])
    driver = EtherNetIpDriver()
    await driver.configure(config)
    await driver.connect()

    result = await driver.write("setpoint", 999999)

    assert result.success is False
    await driver.disconnect()


async def test_write_commerror_propagates(fake_logix: FakeLogixDriver) -> None:
    fake_logix.write_result = CommError("connection reset")
    config = _driver_config([{"id": "setpoint", "tag_name": "Setpoint"}])
    driver = EtherNetIpDriver()
    await driver.configure(config)
    await driver.connect()

    with pytest.raises(CommError):
        await driver.write("setpoint", 1.0)

    await driver.disconnect()


async def test_get_metrics_tracks_reads_and_errors(fake_logix: FakeLogixDriver) -> None:
    fake_logix.read_result = [
        PycommTag("Good", 1, "DINT", None),
        PycommTag("Bad", None, None, "Tag doesn't exist"),
    ]
    config = _driver_config([{"id": "good", "tag_name": "Good"}, {"id": "bad", "tag_name": "Bad"}])
    driver = EtherNetIpDriver()

    await _run_one_cycle(driver, config)

    metrics = driver.get_metrics()
    assert metrics.tag_read_count >= 1
    assert metrics.error_count >= 1


async def test_read_produces_driver_read_span(
    fake_logix: FakeLogixDriver, otel_test_tracer_provider: Any
) -> None:
    fake_logix.read_result = PycommTag("Speed", 1.0, "REAL", None)
    config = _driver_config([{"id": "speed", "tag_name": "Speed"}])
    driver = EtherNetIpDriver()

    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["driver.instance_id"] == "enip_01"
    assert spans[0].attributes["tag.count"] == 1
    assert spans[0].attributes["quality"] == Quality.GOOD.value


async def test_tag_error_produces_bad_quality_span(
    fake_logix: FakeLogixDriver, otel_test_tracer_provider: Any
) -> None:
    fake_logix.read_result = PycommTag("Missing", None, None, "Tag doesn't exist")
    config = _driver_config([{"id": "missing", "tag_name": "Missing"}])
    driver = EtherNetIpDriver()

    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["quality"] == Quality.BAD.value
