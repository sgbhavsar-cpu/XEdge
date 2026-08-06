"""BacnetMstpDriver against the REAL compiled xedge-bacnet-mstp-daemon
binary and a REAL bacnet-stack-backed MS/TP device, bridged over a real
socat virtual serial pair (tests/fixtures/mstp_device.py) -- the actual
MS/TP token-passing protocol over the actual wire format, unlike
tests/unit/test_bacnet_mstp_driver.py's mocked subprocess/socket boundary.

Skipped whenever the compiled binaries or socat aren't available -- true
on a plain `pip install -e ".[dev,test]"` checkout (the daemon needs the
third_party/bacnet-stack submodule fetched and a C toolchain) and in the
main "Unit + Integration Tests" CI job, which deliberately doesn't fetch
the submodule (see .github/workflows/ci.yml's own comment on this).
Exercised for real only by the "BACnet MS/TP Daemon Build" CI job, which
builds both binaries first.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.fixtures.mstp_device import DAEMON_BIN, TEST_ANALOG_VALUE, mstp_binaries_available
from xedge.drivers.bacnet.mstp_client import BacnetMstpDriver
from xedge.drivers.base import DriverConfig, Quality, TagUpdate

pytestmark = pytest.mark.skipif(
    not mstp_binaries_available(),
    reason=(
        "requires the compiled xedge-bacnet-mstp-daemon + mstp_test_server "
        "binaries and socat -- see the 'BACnet MS/TP Daemon Build' CI job"
    ),
)

_OUR_MAC = 0
_OUR_DEVICE_INSTANCE = 3000001
# MS/TP ring formation between two fresh nodes can take several seconds
# (observed ~5s in manual verification) before the first request can even
# be sent -- generous timeouts here avoid flaking on a loaded CI runner.
# The daemon's own main.c has a 10s belt-and-suspenders internal timeout,
# so the driver-side timeout must exceed that.
_REQUEST_TIMEOUT_SECONDS = 12.0
_QUEUE_GET_TIMEOUT_SECONDS = 15.0


def _driver_config(port: str, tags: list[dict[str, Any]]) -> DriverConfig:
    return DriverConfig(
        instance_id="mstp_01",
        driver_type="bacnet_mstp",
        config={
            "port": port,
            "mac_address": _OUR_MAC,
            "device_instance": _OUR_DEVICE_INSTANCE,
            "baud_rate": 38400,
            "request_timeout_seconds": _REQUEST_TIMEOUT_SECONDS,
            "daemon_path": str(DAEMON_BIN),
        },
        tag_groups=[{"id": "group1", "scan_rate_ms": 100, "tags": tags}],
    )


async def _run_one_cycle(driver: BacnetMstpDriver, config: DriverConfig) -> list[TagUpdate]:
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        updates = [
            await asyncio.wait_for(queue.get(), timeout=_QUEUE_GET_TIMEOUT_SECONDS)
            for _ in config.tag_groups[0]["tags"]
        ]
        return updates
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_reads_analog_value(mstp_test_device: tuple[str, int, int]) -> None:
    port, mac, device_instance = mstp_test_device
    config = _driver_config(
        port,
        [
            {
                "id": "temperature_01",
                "device_instance": device_instance,
                "mac_address": mac,
                "object_type": "analog-value",
                "object_instance": 0,
            }
        ],
    )
    driver = BacnetMstpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].value == pytest.approx(TEST_ANALOG_VALUE, abs=1e-3)
    assert updates[0].quality == Quality.GOOD
    assert updates[0].tag_id == "mstp_01/temperature_01"


async def test_reads_binary_value_as_bool(mstp_test_device: tuple[str, int, int]) -> None:
    port, mac, device_instance = mstp_test_device
    config = _driver_config(
        port,
        [
            {
                "id": "pump_running",
                "device_instance": device_instance,
                "mac_address": mac,
                "object_type": "binary-value",
                "object_instance": 0,
            }
        ],
    )
    driver = BacnetMstpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].value is True
    assert updates[0].quality == Quality.GOOD


async def test_unknown_object_maps_to_bad_quality(mstp_test_device: tuple[str, int, int]) -> None:
    port, mac, device_instance = mstp_test_device
    config = _driver_config(
        port,
        [
            {
                "id": "missing",
                "device_instance": device_instance,
                "mac_address": mac,
                "object_type": "analog-value",
                "object_instance": 99,
            }
        ],
    )
    driver = BacnetMstpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].quality == Quality.BAD


async def test_get_metrics_tracks_reads(mstp_test_device: tuple[str, int, int]) -> None:
    port, mac, device_instance = mstp_test_device
    config = _driver_config(
        port,
        [
            {
                "id": "temperature_01",
                "device_instance": device_instance,
                "mac_address": mac,
                "object_type": "analog-value",
                "object_instance": 0,
            }
        ],
    )
    driver = BacnetMstpDriver()
    await _run_one_cycle(driver, config)

    metrics = driver.get_metrics()
    assert metrics.tag_read_count >= 1
    assert metrics.last_successful_read is not None
