"""SnmpAgentService against a real DriverSupervisor + AlarmEngine, queried
by a real pysnmp manager call -- same real-infrastructure bar as the SNMP
manager driver's own tests, just with the roles reversed (this test plays
manager, the code under test plays agent)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    bulk_cmd,
    get_cmd,
)

from xedge.core.alarms import AlarmEngine, AlarmRule
from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverConfig, DriverRegistry, DriverSupervisor
from xedge.drivers.base import Quality, TagUpdate
from xedge.drivers.loopback.driver import LoopbackDriver
from xedge.northbound.snmp_agent import (
    _ALARM_COUNT_BASE_OID,
    _DRIVER_ENTRY_OID,
    SnmpAgentConfig,
    SnmpAgentService,
)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _oid(*parts: int) -> str:
    return ".".join(map(str, parts))


def _running_loopback_config(instance_id: str) -> DriverConfig:
    # A loopback driver with an *empty* tag_groups list has nothing for
    # run()'s asyncio.gather() to wait on, so it returns immediately --
    # DriverSupervisor treats a run() that returns normally as a request
    # to stop supervising (see its own docstring), landing on STOPPED
    # almost instantly rather than staying RUNNING. One trivial tag group
    # keeps its poll loop (and therefore its RUNNING state) alive for the
    # test's duration.
    return DriverConfig(
        instance_id=instance_id,
        driver_type="loopback",
        config={},
        tag_groups=[{"id": "g1", "scan_rate_ms": 1000, "tags": [{"id": "t1"}]}],
    )


@pytest.fixture
async def snmp_test_agent_service() -> AsyncIterator[
    tuple[SnmpAgentService, DriverSupervisor, AlarmEngine, int]
]:
    registry = DriverRegistry()
    registry.register("loopback", LoopbackDriver)
    supervisor = DriverSupervisor(registry, asyncio.Queue[TagUpdate]())
    alarm_engine = AlarmEngine(rules={})
    port = _free_udp_port()
    service = SnmpAgentService(
        SnmpAgentConfig(enabled=True, host="127.0.0.1", port=port, community="public"),
        supervisor,
        alarm_engine,
    )
    await service.start()
    await asyncio.sleep(0.1)
    try:
        yield service, supervisor, alarm_engine, port
    finally:
        for instance_id in list(supervisor.all_status()):
            await supervisor.stop(instance_id)
        await service.stop()


async def _get(port: int, oid: str):  # type: ignore[no-untyped-def]
    manager_engine = SnmpEngine()
    return await get_cmd(
        manager_engine,
        CommunityData("public", mpModel=1),
        await UdpTransportTarget.create(("127.0.0.1", port)),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )


async def _walk(port: int, base_oid: str) -> list[tuple[str, str]]:
    manager_engine = SnmpEngine()
    results: list[tuple[str, str]] = []
    var_bind = ObjectType(ObjectIdentity(base_oid))
    for _ in range(50):
        error_indication, error_status, _error_index, var_binds = await bulk_cmd(
            manager_engine,
            CommunityData("public", mpModel=1),
            await UdpTransportTarget.create(("127.0.0.1", port)),
            ContextData(),
            0,
            1,
            var_bind,
        )
        if error_indication or error_status:
            break
        oid, value = var_binds[0]
        oid_str = str(oid.get_oid())
        if not oid_str.startswith(base_oid):
            break
        results.append((oid_str, value.prettyPrint()))
        var_bind = ObjectType(ObjectIdentity(oid_str))
    return results


async def test_standard_mibii_scalars_are_customized(
    snmp_test_agent_service: tuple[SnmpAgentService, DriverSupervisor, AlarmEngine, int],
) -> None:
    _service, _supervisor, _alarms, port = snmp_test_agent_service
    error_indication, error_status, _error_index, var_binds = await _get(port, "1.3.6.1.2.1.1.1.0")
    assert not error_indication and not error_status
    assert var_binds[0][1].prettyPrint() == "xEdge IIoT Gateway"


async def test_driver_table_reflects_a_running_instance(
    snmp_test_agent_service: tuple[SnmpAgentService, DriverSupervisor, AlarmEngine, int],
) -> None:
    service, supervisor, _alarms, port = snmp_test_agent_service
    supervisor.start(
        _running_loopback_config("loop1")
    )
    await asyncio.sleep(0.1)
    service._sync_once()  # force a sync rather than waiting up to _SYNC_INTERVAL_SECONDS

    driver_type_oid = _oid(*_DRIVER_ENTRY_OID, 2)
    rows = await _walk(port, driver_type_oid)
    assert len(rows) == 1
    assert rows[0][1] == "loopback"


async def test_stopped_instance_row_persists_with_updated_state(
    snmp_test_agent_service: tuple[SnmpAgentService, DriverSupervisor, AlarmEngine, int],
) -> None:
    # DriverSupervisor.stop() (confirmed by reading its source, not
    # assumed) marks a status STOPPED but never removes it from
    # all_status() -- hot_reload.py's own "removed from config" path
    # calls the same stop(), so there is today no real-world scenario
    # where an instance_id actually vanishes from all_status() during
    # a running process. The row therefore persists, with its state
    # column reflecting the change -- see the next test for proof that
    # _remove_driver_row's underlying mechanism works regardless, in
    # case that ever changes.
    service, supervisor, _alarms, port = snmp_test_agent_service
    supervisor.start(
        _running_loopback_config("loop1")
    )
    await asyncio.sleep(0.1)
    service._sync_once()

    driver_state_oid = _oid(*_DRIVER_ENTRY_OID, 3)
    rows = await _walk(port, driver_state_oid)
    assert len(rows) == 1
    assert rows[0][1] == "running"

    await supervisor.stop("loop1")
    service._sync_once()
    rows = await _walk(port, driver_state_oid)
    assert len(rows) == 1
    assert rows[0][1] == "stopped"


async def test_remove_driver_row_mechanism_works(
    snmp_test_agent_service: tuple[SnmpAgentService, DriverSupervisor, AlarmEngine, int],
) -> None:
    # Exercises _remove_driver_row directly (white-box) since
    # DriverSupervisor never actually drops an instance_id from
    # all_status() today (see the test above) -- this proves the
    # RowStatus "destroy" mechanism itself is correct independent of
    # whether anything in this codebase currently triggers it.
    service, supervisor, _alarms, port = snmp_test_agent_service
    supervisor.start(
        _running_loopback_config("loop1")
    )
    await asyncio.sleep(0.1)
    service._sync_once()

    driver_type_oid = _oid(*_DRIVER_ENTRY_OID, 2)
    assert len(await _walk(port, driver_type_oid)) == 1

    service._remove_driver_row("loop1")
    assert len(await _walk(port, driver_type_oid)) == 0


async def test_alarm_counts_reflect_engine_state(
    snmp_test_agent_service: tuple[SnmpAgentService, DriverSupervisor, AlarmEngine, int],
) -> None:
    service, _supervisor, alarm_engine, port = snmp_test_agent_service
    alarm_engine._rules["temp1"] = AlarmRule(tag_id="temp1", high=50.0)

    alarm_engine.evaluate(
        UnifiedTag(
            tag_id="temp1",
            timestamp=datetime.now(UTC),
            value=99.0,
            data_type="float",
            quality=Quality.GOOD,
            source_driver="test",
            source_address="temp1",
        )
    )
    service._sync_once()

    # AlarmState enum declaration order is NORMAL, ACTIVE, ACTIVE_ACKED,
    # so xedgeAlarmCount*'s 1-based index for ACTIVE is 2. The scalar's
    # real name is oid + (0,) -- MibScalarInstance(typeName, instId, ...)
    # concatenates them (see MibTree.__init__) -- so the query needs the
    # trailing .0 instance suffix, not just the "type" OID.
    _ei, _es, _eidx, var_binds = await _get(port, _oid(*_ALARM_COUNT_BASE_OID, 2, 0))
    assert int(var_binds[0][1]) == 1
