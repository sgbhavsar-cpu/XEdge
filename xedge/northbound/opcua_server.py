"""OPC UA server (northbound), FR-UA-001..FR-UA-007.

Implemented directly against `asyncua` for the MVP (ADR-006 §7 amendment,
2026-07-04) — see that amendment for rationale (the open62541 C-extension
binding remains the ADR-006 §3 target for the commercial edition).

Unlike the MQTT/Sparkplug B connector (which batches and publishes), the
OPC UA server holds live state: the information model is built once at
startup from the configured tag list, and each pipeline update writes
straight into the matching node's current value/quality/timestamp —
there is no queue or batching. Security policies beyond None (FR-UA-004),
user authentication (FR-UA-005), and write routing (FR-UA-006) are
later-sprint scope; this MVP serves read-only, unauthenticated access,
matching the driver's read-only scope (system-architecture.md §3.6 notes
unsecured endpoints should be loopback-only in production).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from asyncua import ua as ua_types
from asyncua.common.node import Node
from asyncua.server.server import Server
from asyncua.ua.status_codes import StatusCodes

from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality, TagValue
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_NAMESPACE_URI = "http://xedge/"


class OpcUaServerStateError(RuntimeError):
    """Raised when an operation is attempted out of order (e.g. update_tag()
    before start())."""


@dataclass(frozen=True, slots=True)
class OpcUaServerConfig:
    endpoint_url: str
    server_name: str = "xEdge"
    tag_ids: frozenset[str] = field(default_factory=frozenset)
    """Every tag_id (e.g. "modbus_tcp_01/temperature_01") the server should
    eventually expose — the *set* of tags is still fixed by configuration
    (FR-UA-002: not discovered dynamically), even for tags whose node is
    created lazily rather than pre-built (see `initial_values`). Tags
    already listed in `initial_values` don't need to be repeated here."""
    initial_values: dict[str, TagValue] = field(default_factory=dict)
    """tag_id -> a representative initial value of the tag's actual type,
    for tags pre-built at startup. asyncua's local address space fixes a
    node's OPC UA DataType from its first-written value and rejects later
    writes of a different variant type, so this MUST match what
    update_tag() will later write for that tag (e.g. 0.0 for a scaled/float
    tag, False for a coil, 0 for a plain integer register).

    Tags in `tag_ids` but absent here (e.g. an OPC UA client tag, whose
    remote node's real type can't be known before connecting) get their
    node created lazily on the first update_tag() call instead, using that
    update's actual value type — the only way to avoid guessing wrong and
    corrupting the value (see xedge.core.main._infer_tag_initial_values,
    which only guesses for driver types where the type is knowable from
    config alone)."""


class OpcUaTagServer:
    def __init__(self, config: OpcUaServerConfig) -> None:
        self._config = config
        self._server: Server | None = None
        self._nodes: dict[str, Node] = {}
        self._driver_folders: dict[str, Node] = {}
        self._idx: int | None = None
        self._xedge_folder: Node | None = None
        self._known_tag_ids = frozenset(config.tag_ids) | config.initial_values.keys()
        self.namespace_index: int | None = None

    async def start(self) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self._config.endpoint_url)
        server.set_server_name(self._config.server_name)
        idx = await server.register_namespace(_NAMESPACE_URI)

        self._idx = idx
        self._xedge_folder = await server.get_objects_node().add_folder(idx, "xEdge")
        for tag_id, initial_value in self._config.initial_values.items():
            await self._create_node(tag_id, initial_value)

        await server.start()
        self._server = server
        self.namespace_index = idx
        logger.info(
            "opcua_server.started",
            endpoint_url=self._config.endpoint_url,
            tag_count=len(self._known_tag_ids),
        )

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def _create_node(self, tag_id: str, value: TagValue) -> Node:
        if self._xedge_folder is None or self._idx is None:
            raise OpcUaServerStateError("start() must be called before _create_node()")
        driver_id, _, tag_name = tag_id.partition("/")
        driver_folder = self._driver_folders.get(driver_id)
        if driver_folder is None:
            driver_folder = await self._xedge_folder.add_folder(self._idx, driver_id)
            self._driver_folders[driver_id] = driver_folder
        node = await driver_folder.add_variable(self._idx, tag_name or tag_id, value)
        self._nodes[tag_id] = node
        return node

    async def update_tag(self, tag: UnifiedTag) -> None:
        """Write a pipeline UnifiedTag into its matching OPC UA node,
        creating the node on first sight if it's a known tag_id without a
        pre-built node (see OpcUaServerConfig.tag_ids/initial_values).
        Silently ignores tags outside the configured set — the information
        model's tag set is fixed at startup (FR-UA-002)."""
        if self._server is None:
            raise OpcUaServerStateError("start() must be called before update_tag()")
        node = self._nodes.get(tag.tag_id)
        if node is None:
            if tag.tag_id not in self._known_tag_ids:
                return
            node = await self._create_node(tag.tag_id, tag.value)
        data_value = ua_types.DataValue(
            Value=ua_types.Variant(tag.value),
            StatusCode=_quality_to_status_code(tag.quality),
            # ua.DateTime is a subclass of datetime.datetime with no extra
            # required fields; a plain datetime works fine at runtime.
            SourceTimestamp=tag.timestamp,  # type: ignore[arg-type]
        )
        await node.write_value(data_value)


def _quality_to_status_code(quality: Quality) -> ua_types.StatusCode:
    if quality == Quality.GOOD:
        return ua_types.StatusCode(ua_types.UInt32(StatusCodes.Good))
    if quality == Quality.UNCERTAIN:
        return ua_types.StatusCode(ua_types.UInt32(StatusCodes.Uncertain))
    return ua_types.StatusCode(ua_types.UInt32(StatusCodes.Bad))
