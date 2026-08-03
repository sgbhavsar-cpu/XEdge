"""Standalone SNMP agent for manual testing -- not part of the shipped
xedge package. Built on pysnmp, the same library xedge's own
SnmpClientDriver (and SnmpAgentService) use. Exposes two scalars under a
private-enterprise test subtree, one of which drifts, so you can point
config/examples/snmp-client-example.yaml's `config.host`/`port` and
`tags[].oid` at it without a real SNMP-speaking device.

Usage:
    python tools/mock_snmp_agent.py [--port 1161] [--community public]

(Port defaults to 1161, not the standard 161, since binding 161 requires
root/administrator privileges on most systems.)

Prints the two OIDs on startup -- copy those into your config's `oid`
fields.
"""

from __future__ import annotations

import argparse
import asyncio
import random

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto.api import v2c

_ENTERPRISE_OID = (1, 3, 6, 1, 4, 1, 999999, 9)  # xEdge's own placeholder PEN, sub-arc 9 (demo)
_OID_TEMPERATURE = (*_ENTERPRISE_OID, 1, 0)
_OID_UPTIME_COUNTER = (*_ENTERPRISE_OID, 2, 0)


async def _drift_temperature(instance: object) -> None:
    while True:
        await asyncio.sleep(3)
        value = 200 + random.randint(-15, 15)  # tenths of a degree, e.g. 200 -> 20.0
        instance.syntax = instance.syntax.clone(value)  # type: ignore[attr-defined]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1161)
    parser.add_argument("--community", default="public")
    args = parser.parse_args()

    snmp_engine = engine.SnmpEngine()
    config.add_transport(
        snmp_engine, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode((args.host, args.port))
    )
    config.add_v1_system(snmp_engine, "xedge-mock-area", args.community)
    config.add_vacm_user(
        snmp_engine,
        2,  # SNMPv2c
        "xedge-mock-area",
        "noAuthNoPriv",
        readSubTree=_ENTERPRISE_OID,
        writeSubTree=_ENTERPRISE_OID,
    )
    snmp_context = context.SnmpContext(snmp_engine)
    mib_builder = snmp_context.get_mib_instrum().get_mib_builder()
    mib_scalar, mib_scalar_instance = mib_builder.import_symbols(
        "SNMPv2-SMI", "MibScalar", "MibScalarInstance"
    )

    temperature_instance = mib_scalar_instance(_OID_TEMPERATURE[:-1], (0,), v2c.Integer32(200))
    uptime_instance = mib_scalar_instance(_OID_UPTIME_COUNTER[:-1], (0,), v2c.Integer32(0))
    mib_builder.export_symbols(
        "__XEDGE_MOCK_MIB",
        mib_scalar(_OID_TEMPERATURE[:-1], v2c.Integer32(200)),
        temperature_instance,
        mib_scalar(_OID_UPTIME_COUNTER[:-1], v2c.Integer32(0)),
        uptime_instance,
    )

    cmdrsp.GetCommandResponder(snmp_engine, snmp_context)
    cmdrsp.NextCommandResponder(snmp_engine, snmp_context)
    cmdrsp.BulkCommandResponder(snmp_engine, snmp_context)
    snmp_engine.open_dispatcher()

    oid_str = ".".join(map(str, _OID_TEMPERATURE))
    counter_oid_str = ".".join(map(str, _OID_UPTIME_COUNTER))
    print(f"SNMP mock agent listening on {args.host}:{args.port} (community: {args.community})")
    print(f"  temperature (drifts every 3s): {oid_str}")
    print(f"  counter (static 0):            {counter_oid_str}")

    drift_task = asyncio.ensure_future(_drift_temperature(temperature_instance))
    try:
        await asyncio.Event().wait()  # run until Ctrl+C
    finally:
        drift_task.cancel()
        snmp_engine.close_dispatcher()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
