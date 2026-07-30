"""snmp_alarm_notification_loop against a real local notification receiver
(the same `ntfrcv.NotificationReceiver` mechanism confirmed directly
before xedge.drivers.snmp.receiver was written) -- proves an actual UDP
TRAP/INFORM round trip, not just that the right Python call was made."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config as snmp_config
from pysnmp.entity import engine as snmp_engine_module
from pysnmp.entity.rfc3413 import ntfrcv

from xedge.core.alarms import AlarmEngine, AlarmRule, AlarmState
from xedge.core.pipeline import UnifiedTag
from xedge.core.snmp_notify import (
    SnmpNotifyConfig,
    SnmpNotifyStatus,
    SnmpTrapDestination,
    send_alarm_notification,
    snmp_alarm_notification_loop,
)
from xedge.drivers.base import Quality


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_until(predicate, timeout_seconds: float = 3.0) -> None:
    """Polls rather than a flat sleep -- this test's own timing (a fixed
    sleep proved flaky under load: passed alone, intermittently missed the
    second notification when run alongside this file's other tests) spans
    three independent async hops (this coroutine's own wait, the loop's
    poll tick, and fire-and-forget UDP delivery), none of which this test
    controls directly."""
    async with asyncio.timeout(timeout_seconds):
        while not predicate():  # noqa: ASYNC110 -- polling an arbitrary predicate, not one Event
            await asyncio.sleep(0.02)


class _RecordingReceiver:
    def __init__(self, port: int, community: str = "public") -> None:
        self.port = port
        self.received: list[dict[str, str]] = []
        self.engine = snmp_engine_module.SnmpEngine()
        snmp_config.add_transport(
            self.engine, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode(("127.0.0.1", port))
        )
        snmp_config.add_v1_system(self.engine, "test-area", community)
        ntfrcv.NotificationReceiver(self.engine, self._callback)
        self.engine.open_dispatcher()

    def _callback(
        self, snmp_engine, state_reference, context_engine_id, context_name, var_binds, cb_ctx
    ):  # noqa: ANN001, E501
        self.received.append({str(name): value.prettyPrint() for name, value in var_binds})

    def close(self) -> None:
        self.engine.close_dispatcher()


@pytest.fixture
async def recording_receiver() -> AsyncIterator[_RecordingReceiver]:
    receiver = _RecordingReceiver(_free_udp_port())
    await asyncio.sleep(0.1)
    try:
        yield receiver
    finally:
        receiver.close()


def _alarm_status(tag_id: str, state: AlarmState) -> object:
    engine = AlarmEngine(rules={tag_id: AlarmRule(tag_id=tag_id, high=50.0)})
    engine.evaluate(
        UnifiedTag(
            tag_id=tag_id,
            timestamp=datetime.now(UTC),
            value=99.0 if state != AlarmState.NORMAL else 1.0,
            data_type="float",
            quality=Quality.GOOD,
            source_driver="test",
            source_address=tag_id,
        )
    )
    return engine.all_status()[tag_id]


async def test_send_alarm_notification_reaches_a_real_receiver(
    recording_receiver: _RecordingReceiver,
) -> None:
    config = SnmpNotifyConfig(
        enabled=True,
        destinations=(SnmpTrapDestination(host="127.0.0.1", port=recording_receiver.port),),
    )
    status = SnmpNotifyStatus()
    alarm_status = _alarm_status("temp1", AlarmState.ACTIVE)

    await send_alarm_notification(config, alarm_status, status, raised=True)
    # TRAP is fire-and-forget UDP -- send_notification returning without
    # error only confirms the local send, not that the receiver has
    # processed it yet (confirmed by this test itself failing without
    # this wait on the first attempt). INFORM's own confirmed round trip
    # needs no such wait -- see the test below.
    await _wait_until(lambda: len(recording_receiver.received) >= 1)

    assert status.last_send_ok is True
    assert status.notifications_sent == 1
    assert len(recording_receiver.received) == 1
    varbinds = recording_receiver.received[0]
    assert varbinds["1.3.6.1.4.1.999999.4.1"] == "temp1"
    assert varbinds["1.3.6.1.4.1.999999.4.4"] == "active"


async def test_send_alarm_notification_via_inform(recording_receiver: _RecordingReceiver) -> None:
    config = SnmpNotifyConfig(
        enabled=True,
        destinations=(
            SnmpTrapDestination(
                host="127.0.0.1", port=recording_receiver.port, notify_type="inform"
            ),
        ),
    )
    status = SnmpNotifyStatus()
    alarm_status = _alarm_status("temp1", AlarmState.ACTIVE)

    await send_alarm_notification(config, alarm_status, status, raised=True)

    assert status.last_send_ok is True
    assert len(recording_receiver.received) == 1


async def test_inform_records_failure_for_unreachable_destination() -> None:
    # INFORM, not TRAP: only INFORM's confirmed round trip can ever detect
    # an unreachable destination -- see the next test for why TRAP cannot,
    # in principle, not just in this implementation.
    config = SnmpNotifyConfig(
        enabled=True,
        destinations=(SnmpTrapDestination(host="127.0.0.1", port=1, notify_type="inform"),),
    )
    status = SnmpNotifyStatus()
    alarm_status = _alarm_status("temp1", AlarmState.ACTIVE)

    await send_alarm_notification(config, alarm_status, status, raised=True)

    assert status.last_send_ok is False
    assert status.last_error is not None
    assert status.notifications_sent == 0


async def test_trap_reports_sent_even_to_an_unreachable_destination() -> None:
    # A real, verified property of TRAP (RFC 1905), not a gap in this
    # module: TRAP is fire-and-forget UDP with no delivery acknowledgement
    # at the protocol level, so send_notification (and therefore
    # send_alarm_notification) cannot distinguish "delivered" from
    # "sent into the void" for notify_type: trap -- confirmed here against
    # a destination nothing is listening on. Use notify_type: inform (see
    # the test above) wherever delivery confirmation actually matters.
    config = SnmpNotifyConfig(
        enabled=True,
        destinations=(SnmpTrapDestination(host="127.0.0.1", port=1, notify_type="trap"),),
    )
    status = SnmpNotifyStatus()
    alarm_status = _alarm_status("temp1", AlarmState.ACTIVE)

    await send_alarm_notification(config, alarm_status, status, raised=True)

    assert status.last_send_ok is True
    assert status.notifications_sent == 1


async def test_loop_notifies_on_raise_and_clear_but_not_on_ack(
    recording_receiver: _RecordingReceiver,
) -> None:
    alarm_engine = AlarmEngine(rules={"temp1": AlarmRule(tag_id="temp1", high=50.0)})
    config = SnmpNotifyConfig(
        enabled=True,
        destinations=(SnmpTrapDestination(host="127.0.0.1", port=recording_receiver.port),),
    )
    status = SnmpNotifyStatus()
    loop_task = asyncio.create_task(
        snmp_alarm_notification_loop(alarm_engine, config, status, poll_interval_seconds=0.05)
    )
    try:

        def _sample(value: float) -> UnifiedTag:
            return UnifiedTag(
                tag_id="temp1",
                timestamp=datetime.now(UTC),
                value=value,
                data_type="float",
                quality=Quality.GOOD,
                source_driver="test",
                source_address="temp1",
            )

        alarm_engine.evaluate(_sample(99.0))  # NORMAL -> ACTIVE: notify
        await _wait_until(lambda: len(recording_receiver.received) >= 1)
        alarm_engine.acknowledge("temp1", "operator")  # ACTIVE -> ACTIVE_ACKED: no boundary crossed
        await asyncio.sleep(0.3)  # nothing to poll-wait for: this step must send zero notifications
        alarm_engine.evaluate(_sample(1.0))  # ACTIVE_ACKED -> NORMAL: notify
        await _wait_until(lambda: len(recording_receiver.received) >= 2)

        assert len(recording_receiver.received) == 2
        assert recording_receiver.received[0]["1.3.6.1.4.1.999999.4.4"] == "active"
        assert recording_receiver.received[1]["1.3.6.1.4.1.999999.4.4"] == "normal"
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
