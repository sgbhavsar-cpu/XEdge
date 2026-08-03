"""Real pysnmp-backed SNMP agent used only to test our own manager driver
end-to-end (ADR-006 black-box-oracle precedent — the same role
fake_modbus_server.py, opcua_server.py, and bacnet_device.py play for their
own protocols). Not shipped, not imported by xedge itself.

A plain `MibScalar` defaults to `maxAccess = "read-only"` (confirmed by
reading pysnmp's own `SNMPv2-SMI.py` directly) independently of VACM's own
read/write subtree grants — a scalar needs *both* a VACM write grant *and*
`maxAccess = "read-write"` to actually accept SET, which this fixture's
`_WritableMibScalar` provides for every OID it registers.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto.api import v2c

TEST_ENTERPRISE_OID = (1, 3, 6, 1, 4, 1, 99999)
COMMUNITY = "public"

# Sub-OIDs under TEST_ENTERPRISE_OID this fixture pre-registers. Anything
# else under the enterprise subtree (e.g. .99.0) is deliberately
# unregistered, for a real "no such object" response.
OID_COUNTER = ".".join(map(str, (*TEST_ENTERPRISE_OID, 1, 0)))
OID_TEXT = ".".join(map(str, (*TEST_ENTERPRISE_OID, 2, 0)))
OID_LIVE = ".".join(map(str, (*TEST_ENTERPRISE_OID, 3, 0)))
OID_MISSING = ".".join(map(str, (*TEST_ENTERPRISE_OID, 99, 0)))


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeSnmpAgent:
    def __init__(self, community: str = COMMUNITY) -> None:
        self.port = free_udp_port()
        self.engine = engine.SnmpEngine()
        config.add_transport(
            self.engine,
            udp.DOMAIN_NAME,
            udp.UdpTransport().open_server_mode(("127.0.0.1", self.port)),
        )
        config.add_v1_system(self.engine, "test-area", community)
        config.add_vacm_user(
            self.engine,
            2,  # SNMPv2c
            "test-area",
            "noAuthNoPriv",
            readSubTree=TEST_ENTERPRISE_OID,
            writeSubTree=TEST_ENTERPRISE_OID,
        )
        snmp_context = context.SnmpContext(self.engine)
        mib_builder = snmp_context.get_mib_instrum().get_mib_builder()
        mib_scalar, mib_scalar_instance = mib_builder.import_symbols(
            "SNMPv2-SMI", "MibScalar", "MibScalarInstance"
        )

        class _WritableMibScalar(mib_scalar):  # type: ignore[misc,valid-type]
            maxAccess = "read-write"

        self._instances: dict[int, object] = {}
        scalars = ((1, v2c.Integer32(0)), (2, v2c.OctetString("")), (3, v2c.Integer32(0)))
        for sub_id, initial in scalars:
            oid = (*TEST_ENTERPRISE_OID, sub_id)
            instance = mib_scalar_instance(oid, (0,), initial)
            mib_builder.export_symbols(
                f"__TEST_MIB_{sub_id}", _WritableMibScalar(oid, initial), instance
            )
            self._instances[sub_id] = instance

        cmdrsp.GetCommandResponder(self.engine, snmp_context)
        cmdrsp.NextCommandResponder(self.engine, snmp_context)
        cmdrsp.BulkCommandResponder(self.engine, snmp_context)
        cmdrsp.SetCommandResponder(self.engine, snmp_context)
        self.engine.open_dispatcher()  # no-op: our event loop is already running

    def set_counter(self, value: int) -> None:
        self._instances[1].syntax = self._instances[1].syntax.clone(value)  # type: ignore[attr-defined]

    def set_text(self, value: str) -> None:
        self._instances[2].syntax = self._instances[2].syntax.clone(value)  # type: ignore[attr-defined]

    def set_live(self, value: int) -> None:
        """A scalar the *test* mutates directly between polls, simulating
        the device's own state changing outside of any SNMP request --
        distinct from `set_counter`, which exists to be SET *by the
        driver under test*."""
        self._instances[3].syntax = self._instances[3].syntax.clone(value)  # type: ignore[attr-defined]

    def close(self) -> None:
        self.engine.close_dispatcher()


@pytest.fixture
async def snmp_test_agent() -> AsyncIterator[FakeSnmpAgent]:
    # An async fixture, not a plain sync one, deliberately -- constructing
    # pysnmp's asyncio transport outside of pytest-asyncio's own running
    # loop context (confirmed empirically: a sync fixture reproduced an
    # indefinite hang on the very first request) leaves it bound to the
    # wrong event loop. Same reasoning as bacnet_device.py's fixture,
    # which settles with a short sleep for the same class of reason.
    agent = FakeSnmpAgent()
    await asyncio.sleep(0.1)
    try:
        yield agent
    finally:
        agent.close()
