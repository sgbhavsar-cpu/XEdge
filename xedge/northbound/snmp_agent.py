"""SNMP agent (Sprint C8, XEDGE-482; ADR-012 §2): xEdge itself pollable by
the customer's NMS, exposing system status plus live driver and alarm
status via SNMP.

A listening *service*, not a client, same shape as
`xedge.northbound.mqtt_broker.MqttBrokerService` — an SNMP manager (e.g.
Net-SNMP, a customer's NMS) polls *this*, the reverse of
`xedge.drivers.snmp.client.SnmpClientDriver`.

**Standard MIB-II needs no code at all**: querying a freshly constructed
`SnmpContext` with zero custom instrumentation already answers
`sysDescr`/`sysUpTime`/`sysName` (confirmed directly, not assumed) —
`pysnmp` bundles a working `SNMPv2-MIB` instrumentation by default.
`sysDescr`/`sysName` are overridden below to something xEdge-specific
rather than leaking "PySNMP engine version ..." to a customer's NMS;
`sysUpTime` is left alone since it already tracks real process uptime.

**Placeholder Private Enterprise Number** (XEDGE-DR-001 Q-8, resolved
2026-07-30): xEdge has no IANA-registered PEN today. Every xEdge-specific
OID below lives under `_PLACEHOLDER_ENTERPRISE_OID` — this MUST be
replaced with a real assigned PEN before this agent is relied on for
production monitoring. Swapping it is a one-constant change here, not a
redesign, by design.

**The driver table is a live view, kept in sync by a background loop, not
a table populated once** (XEDGE-DR-001 Q-9 — "full live per-driver table"
was chosen over fixed per-state counts once the added complexity below
was disclosed). Confirmed directly against a real running agent before
writing this: a `MibTableRow` needs a `RowStatus` column and
`write_variables(..., "createAndGo")` / `(..., "destroy")` to actually
appear/disappear from a GETBULK/GETNEXT walk — updating a row's other
column values in place does not add or remove it. `_sync_loop` diffs
`DriverSupervisor.all_status()`'s current instance_ids against this
table's own instrumented rows every `_SYNC_INTERVAL_SECONDS`.

**Alarm counts are fixed per-`AlarmState` scalars, not a table**:
`AlarmStatus` has no identity distinct from its own `tag_id` (already
visible via the driver/tag REST API and Web UI), and "is anything
alarming" is what an NMS polling a gateway actually wants — unlike driver
health, where a customer's NMS genuinely needs to know *which* instance
misbehaves without a second tool.

This agent is deliberately **read-only from the network's perspective**:
every column it registers is `maxAccess="read-only"`, and no
`SetCommandResponder` is registered at all — the only way any of this
data changes is `_sync_loop` observing `DriverSupervisor`/`AlarmEngine`
directly, never an inbound SNMP SET.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config as snmp_config
from pysnmp.entity import engine as snmp_engine_module
from pysnmp.entity.rfc3413 import cmdrsp
from pysnmp.entity.rfc3413 import context as snmp_context_module
from pysnmp.proto.api import v2c

from xedge.core.alarms import AlarmEngine, AlarmState
from xedge.core.supervisor import DriverSupervisor
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

# See module docstring: not a real IANA-assigned Private Enterprise
# Number. Replace before relying on this agent in production.
_PLACEHOLDER_ENTERPRISE_OID: tuple[int, ...] = (1, 3, 6, 1, 4, 1, 999999)
_DRIVER_TABLE_OID = (*_PLACEHOLDER_ENTERPRISE_OID, 1)
_DRIVER_ENTRY_OID = (*_DRIVER_TABLE_OID, 1)
_ALARM_COUNT_BASE_OID = (*_PLACEHOLDER_ENTERPRISE_OID, 2)

_DEFAULT_PORT = 161
_SYNC_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class SnmpAgentConfig:
    enabled: bool = False
    host: str = "0.0.0.0"  # nosec B104 — an agent must accept polls from the NMS on the network
    port: int = _DEFAULT_PORT
    community: str = "public"


class SnmpAgentStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


class SnmpAgentService:
    """Owns the embedded SNMP agent's start/stop lifecycle, mirroring
    `MqttBrokerService`'s shape."""

    def __init__(
        self,
        config: SnmpAgentConfig,
        supervisor: DriverSupervisor,
        alarm_engine: AlarmEngine | None,
    ) -> None:
        self._config = config
        self._supervisor = supervisor
        self._alarm_engine = alarm_engine
        self._engine: snmp_engine_module.SnmpEngine | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._known_instance_ids: set[str] = set()
        self._mib_instrum: Any = None
        self._driver_entry: Any = None
        self._driver_type_col: Any = None
        self._driver_state_col: Any = None
        self._driver_conn_state_col: Any = None
        self._driver_row_status_col: Any = None
        self._alarm_count_instances: dict[AlarmState, Any] = {}

    async def start(self) -> None:
        if self._engine is not None:
            raise SnmpAgentStateError("start() already called")
        cfg = self._config
        engine_obj = snmp_engine_module.SnmpEngine()
        snmp_config.add_transport(
            engine_obj, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode((cfg.host, cfg.port))
        )
        snmp_config.add_v1_system(engine_obj, "xedge-agent", cfg.community)
        # (1, 3, 6, 1) covers both standard MIB-II (1.3.6.1.2.1) and our
        # placeholder enterprise subtree (1.3.6.1.4.1.999999) in one grant.
        snmp_config.add_vacm_user(
            engine_obj, 2, "xedge-agent", "noAuthNoPriv", readSubTree=(1, 3, 6, 1)
        )
        snmp_context = snmp_context_module.SnmpContext(engine_obj)
        self._register_mib_objects(snmp_context)

        cmdrsp.GetCommandResponder(engine_obj, snmp_context)
        cmdrsp.NextCommandResponder(engine_obj, snmp_context)
        cmdrsp.BulkCommandResponder(engine_obj, snmp_context)
        engine_obj.open_dispatcher()
        self._engine = engine_obj
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("snmp_agent.started", host=cfg.host, port=cfg.port)

    async def stop(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)
            self._sync_task = None
        if self._engine is not None:
            self._engine.close_dispatcher()
            self._engine = None

    def _register_mib_objects(self, snmp_context: Any) -> None:
        self._mib_instrum = snmp_context.get_mib_instrum()
        mib_builder = self._mib_instrum.get_mib_builder()

        sys_descr, sys_name = mib_builder.import_symbols("__SNMPv2-MIB", "sysDescr", "sysName")
        sys_descr.syntax = sys_descr.syntax.clone("xEdge IIoT Gateway")
        sys_name.syntax = sys_name.syntax.clone(self._supervisor_hostname())

        mib_table, mib_table_row, mib_table_column = mib_builder.import_symbols(
            "SNMPv2-SMI", "MibTable", "MibTableRow", "MibTableColumn"
        )
        (row_status,) = mib_builder.import_symbols("SNMPv2-TC", "RowStatus")
        mib_builder.export_symbols(
            "__XEDGE_AGENT_MIB",
            driverTable=mib_table(_DRIVER_TABLE_OID),
            driverEntry=mib_table_row(_DRIVER_ENTRY_OID).setIndexNames(
                (0, "__XEDGE_AGENT_MIB", "driverInstanceId")
            ),
            driverInstanceId=mib_table_column(
                (*_DRIVER_ENTRY_OID, 1), v2c.OctetString()
            ).setMaxAccess("read-only"),
            driverType=mib_table_column((*_DRIVER_ENTRY_OID, 2), v2c.OctetString()).setMaxAccess(
                "read-only"
            ),
            driverState=mib_table_column((*_DRIVER_ENTRY_OID, 3), v2c.OctetString()).setMaxAccess(
                "read-only"
            ),
            driverConnectivityState=mib_table_column(
                (*_DRIVER_ENTRY_OID, 4), v2c.OctetString()
            ).setMaxAccess("read-only"),
            driverRowStatus=mib_table_column(
                (*_DRIVER_ENTRY_OID, 5), row_status("notExists")
            ).setMaxAccess("read-only"),
        )
        (
            self._driver_entry,
            self._driver_type_col,
            self._driver_state_col,
            self._driver_conn_state_col,
            self._driver_row_status_col,
        ) = mib_builder.import_symbols(
            "__XEDGE_AGENT_MIB",
            "driverEntry",
            "driverType",
            "driverState",
            "driverConnectivityState",
            "driverRowStatus",
        )

        mib_scalar, mib_scalar_instance = mib_builder.import_symbols(
            "SNMPv2-SMI", "MibScalar", "MibScalarInstance"
        )
        alarm_count_symbols: dict[str, Any] = {}
        for index, state in enumerate(AlarmState, start=1):
            oid = (*_ALARM_COUNT_BASE_OID, index)
            instance = mib_scalar_instance(oid, (0,), v2c.Integer32(0))
            alarm_count_symbols[f"alarmCountScalar{index}"] = mib_scalar(
                oid, v2c.Integer32(0)
            ).setMaxAccess("read-only")
            alarm_count_symbols[f"alarmCountInstance{index}"] = instance
            self._alarm_count_instances[state] = instance
        mib_builder.export_symbols("__XEDGE_AGENT_ALARM_MIB", **alarm_count_symbols)

    def _supervisor_hostname(self) -> str:
        # A stable, human-recognizable sysName beats pysnmp's own default
        # (empty string) -- there is no configured device name concept in
        # xEdge yet, so this is deliberately minimal rather than invented.
        return "xedge"

    async def _sync_loop(self) -> None:
        while True:
            try:
                self._sync_once()
            except Exception as exc:  # noqa: BLE001 -- a sync failure must not kill this loop
                logger.warning("snmp_agent.sync_failed", error=str(exc))
            await asyncio.sleep(_SYNC_INTERVAL_SECONDS)

    def _sync_once(self) -> None:
        current = self._supervisor.all_status()
        current_ids = set(current)

        # RowStatus (RFC 2579) is its own state machine: `createAndGo` is
        # only a valid transition from notExists/notInService, not from
        # the `active` state a row settles into right after creation --
        # confirmed the hard way (InconsistentValueError) resending it on
        # every sync tick for an already-existing row. A new row is
        # created once; an existing one only has its non-status columns
        # updated afterwards.
        for instance_id in current_ids - self._known_instance_ids:
            self._create_driver_row(current[instance_id])
        for instance_id in current_ids & self._known_instance_ids:
            self._update_driver_row(current[instance_id])
        for instance_id in self._known_instance_ids - current_ids:
            self._remove_driver_row(instance_id)
        self._known_instance_ids = current_ids

        self._sync_alarm_counts()

    def _create_driver_row(self, status: Any) -> None:
        row_instance_id = self._driver_entry.getInstIdFromIndices(status.instance_id)
        self._mib_instrum.write_variables(
            (self._driver_type_col.name + row_instance_id, status.driver_type),
            (self._driver_state_col.name + row_instance_id, status.state.value),
            (
                self._driver_conn_state_col.name + row_instance_id,
                status.connectivity_state.value,
            ),
            (self._driver_row_status_col.name + row_instance_id, "createAndGo"),
        )

    def _update_driver_row(self, status: Any) -> None:
        row_instance_id = self._driver_entry.getInstIdFromIndices(status.instance_id)
        self._mib_instrum.write_variables(
            (self._driver_type_col.name + row_instance_id, status.driver_type),
            (self._driver_state_col.name + row_instance_id, status.state.value),
            (
                self._driver_conn_state_col.name + row_instance_id,
                status.connectivity_state.value,
            ),
        )

    def _remove_driver_row(self, instance_id: str) -> None:
        row_instance_id = self._driver_entry.getInstIdFromIndices(instance_id)
        self._mib_instrum.write_variables(
            (self._driver_row_status_col.name + row_instance_id, "destroy")
        )

    def _sync_alarm_counts(self) -> None:
        counts = dict.fromkeys(AlarmState, 0)
        if self._alarm_engine is not None:
            for status in self._alarm_engine.all_status().values():
                counts[status.state] += 1
        for state, instance in self._alarm_count_instances.items():
            instance.syntax = instance.syntax.clone(counts[state])
