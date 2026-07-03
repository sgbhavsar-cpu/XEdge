"""XEDGE-027: full path Modbus TCP -> pipeline -> ring buffer -> Sparkplug B
-> MQTT broker, verified by decoding the captured payload — the Milestone M1
demonstration, reassembled as an automated test.

Every hop is the real production code (xedge.core.main's own
`_pipeline_to_buffer`, the real `NorthboundDispatcher`/`MqttSparkplugConnector`);
only the Modbus device and the MQTT broker are test doubles (an in-house
fake server and amqtt, respectively — see ADR-006 / license-audit.md).
"""

from __future__ import annotations

import asyncio

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from pysparkplug._payload import NData

from tests.fixtures.fake_modbus_server import FakeModbusServer
from xedge.core.main import _pipeline_to_buffer
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.drivers.base import DriverConfig, TagUpdate
from xedge.drivers.modbus.tcp import ModbusTcpDriver
from xedge.northbound.dispatcher import NorthboundDispatcher
from xedge.northbound.mqtt import MqttSparkplugConnector, SparkplugConnectorConfig
from xedge.store.ring_buffer import RingBufferManager


class _NdataCapture:
    def __init__(self, host: str, port: int) -> None:
        self.messages: list[bytes] = []
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self._client.on_message = lambda _c, _u, msg: self.messages.append(msg.payload)
        self._client.connect(host, port, 10)
        self._client.subscribe("spBv1.0/+/NDATA/+")
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


async def test_end_to_end_modbus_to_sparkplug_to_mqtt(mqtt_broker: tuple[str, int]) -> None:
    broker_host, broker_port = mqtt_broker

    # --- Arrange: a real Modbus TCP device (test double) with a known value ---
    modbus_server = FakeModbusServer()
    await modbus_server.start()

    # --- Arrange: driver framework, exactly as main.py wires it ---
    tag_queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=1000)
    registry = DriverRegistry()
    registry.register("modbus_tcp", ModbusTcpDriver)
    supervisor = DriverSupervisor(registry, tag_queue)

    ring_buffers = RingBufferManager()
    buffer_task = asyncio.create_task(_pipeline_to_buffer(tag_queue, ring_buffers))

    connector = MqttSparkplugConnector(
        SparkplugConnectorConfig(
            host=broker_host,
            port=broker_port,
            group_id="factory1",
            edge_node_id="edge01",
        )
    )
    dispatcher = NorthboundDispatcher(connector, ring_buffers, publish_interval_seconds=0.05)
    dispatcher_task = asyncio.create_task(dispatcher.run())

    capture = _NdataCapture(broker_host, broker_port)
    await asyncio.sleep(0.2)  # let the NDATA subscription land

    try:
        # --- Act: the device reports a real value; the driver starts polling ---
        modbus_server.holding_registers[0] = 8531
        supervisor.start(
            DriverConfig(
                instance_id="modbus_tcp_01",
                driver_type="modbus_tcp",
                config={"host": modbus_server.host, "port": modbus_server.port},
                tag_groups=[
                    {
                        "id": "analog",
                        "scan_rate_ms": 50,
                        "tags": [
                            {
                                "id": "temperature_01",
                                "function_code": "read_holding_registers",
                                "address": 0,
                            }
                        ],
                    }
                ],
            )
        )

        # --- Assert: the value arrives at the MQTT broker as a valid Sparkplug B NDATA ---
        async def _first_ndata_with_our_tag() -> NData:
            while True:
                for raw in list(capture.messages):
                    decoded = NData.decode(raw)
                    if (
                        decoded.metrics
                        and decoded.metrics[0].name == "modbus_tcp_01/temperature_01"
                    ):
                        return decoded
                await asyncio.sleep(0.05)

        decoded = await asyncio.wait_for(_first_ndata_with_our_tag(), timeout=5.0)
        assert decoded.metrics[0].value == 8531
    finally:
        await supervisor.stop_all()
        buffer_task.cancel()
        dispatcher_task.cancel()
        await asyncio.gather(buffer_task, dispatcher_task, return_exceptions=True)
        await dispatcher.stop()
        capture.stop()
        await modbus_server.stop()
