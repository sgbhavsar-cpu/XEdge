"""SnmpTrapReceiverDriver against a real pysnmp notification send, using
the exact `send_notification` mechanism confirmed directly against a real
receiver before this driver was written -- same real-infrastructure bar
as the rest of this package's SNMP tests."""

from __future__ import annotations

import asyncio
import socket

import pytest
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    NotificationType,
    ObjectIdentity,
    ObjectType,
    OctetString,
    SnmpEngine,
    UdpTransportTarget,
    send_notification,
)

from xedge.drivers.base import DriverConfig, TagUpdate
from xedge.drivers.snmp.client import SnmpConfigError
from xedge.drivers.snmp.receiver import SnmpTrapReceiverDriver

_TAG_OID = "1.3.6.1.4.1.999999.9.1"
_OTHER_OID = "1.3.6.1.4.1.999999.9.2"
_UNMAPPED_OID = "1.3.6.1.4.1.999999.9.99"


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _driver_config(tags: list[dict], port: int, **config_overrides: object) -> DriverConfig:
    return DriverConfig(
        instance_id="trap_rx_01",
        driver_type="snmp_trap_receiver",
        config={
            "host": "127.0.0.1",
            "port": port,
            "version": "v2c",
            "community": "public",
            **config_overrides,
        },
        tag_groups=[{"id": "group1", "tags": tags}],
    )


async def _send_notification(
    port: int, notify_type: str, *varbinds: ObjectType, community: str = "public"
) -> tuple:
    manager_engine = SnmpEngine()
    return await send_notification(
        manager_engine,
        CommunityData(community, mpModel=1),
        await UdpTransportTarget.create(("127.0.0.1", port)),
        ContextData(),
        notify_type,
        NotificationType(ObjectIdentity("1.3.6.1.4.1.999999.0.1")).add_varbinds(*varbinds),
    )


async def test_receives_trap_and_maps_matching_varbind_to_tag() -> None:
    port = _free_udp_port()
    config = _driver_config([{"id": "alarm_tag", "trap_oid": _TAG_OID}], port=port)
    driver = SnmpTrapReceiverDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        await asyncio.sleep(0.1)
        error_indication, error_status, _idx, _vb = await _send_notification(
            port, "trap", ObjectType(ObjectIdentity(_TAG_OID), OctetString("active"))
        )
        assert not error_indication and not error_status

        update = await asyncio.wait_for(queue.get(), timeout=3.0)
        assert update.tag_id == "trap_rx_01/alarm_tag"
        assert update.value == "active"
        assert update.source_address == _TAG_OID
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_receives_inform_and_maps_matching_varbind_to_tag() -> None:
    # INFORM (confirmed delivery) uses the exact same NotificationReceiver
    # callback as TRAP -- confirmed directly before writing this driver,
    # not assumed -- so this only needs to prove that path too, not
    # re-derive the mapping logic already covered by the trap test above.
    port = _free_udp_port()
    config = _driver_config([{"id": "alarm_tag", "trap_oid": _TAG_OID}], port=port)
    driver = SnmpTrapReceiverDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        await asyncio.sleep(0.1)
        error_indication, error_status, _idx, _vb = await _send_notification(
            port, "inform", ObjectType(ObjectIdentity(_TAG_OID), OctetString("cleared"))
        )
        assert not error_indication and not error_status

        update = await asyncio.wait_for(queue.get(), timeout=3.0)
        assert update.value == "cleared"
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_unmapped_varbind_is_dropped_without_error() -> None:
    port = _free_udp_port()
    config = _driver_config([{"id": "alarm_tag", "trap_oid": _TAG_OID}], port=port)
    driver = SnmpTrapReceiverDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        await asyncio.sleep(0.1)
        await _send_notification(
            port, "trap", ObjectType(ObjectIdentity(_UNMAPPED_OID), OctetString("noise"))
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=1.0)
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_only_matching_varbind_is_mapped_from_a_multi_varbind_notification() -> None:
    port = _free_udp_port()
    config = _driver_config([{"id": "alarm_tag", "trap_oid": _OTHER_OID}], port=port)
    driver = SnmpTrapReceiverDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        await asyncio.sleep(0.1)
        await _send_notification(
            port,
            "trap",
            ObjectType(ObjectIdentity(_TAG_OID), OctetString("ignored")),
            ObjectType(ObjectIdentity(_OTHER_OID), OctetString("matched")),
        )
        update = await asyncio.wait_for(queue.get(), timeout=3.0)
        assert update.value == "matched"
        assert queue.empty()
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_write_returns_not_supported() -> None:
    port = _free_udp_port()
    driver = SnmpTrapReceiverDriver()
    await driver.configure(_driver_config([{"id": "t1", "trap_oid": _TAG_OID}], port=port))
    await driver.connect()
    try:
        result = await driver.write("t1", "x")
        assert result.success is False
    finally:
        await driver.disconnect()


async def test_v3_privacy_without_auth_rejected_at_connect() -> None:
    port = _free_udp_port()
    driver = SnmpTrapReceiverDriver()
    config = _driver_config(
        [{"id": "t1", "trap_oid": _TAG_OID}],
        port=port,
        version="v3",
        v3_username="operator",
        v3_priv_protocol="aes128",
        v3_priv_password="privpassword",
    )
    del config.config["community"]
    await driver.configure(config)

    with pytest.raises(SnmpConfigError):
        await driver.connect()


async def test_get_metrics_tracks_reads() -> None:
    port = _free_udp_port()
    config = _driver_config([{"id": "alarm_tag", "trap_oid": _TAG_OID}], port=port)
    driver = SnmpTrapReceiverDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        await asyncio.sleep(0.1)
        await _send_notification(
            port, "trap", ObjectType(ObjectIdentity(_TAG_OID), OctetString("x"))
        )
        await asyncio.wait_for(queue.get(), timeout=3.0)
        assert driver.get_metrics().tag_read_count >= 1
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()
