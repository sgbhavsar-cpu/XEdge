"""Standalone Modbus TCP simulator for manual testing — not part of the
shipped xedge package. Reuses the project's own in-house test fixture
(tests/fixtures/fake_modbus_server.py — the same server the integration
test suite already validates the Modbus TCP driver against), so you can
point config/examples/modbus-tcp-example.yaml at it without any real PLC.

(Not pymodbus's own server: pymodbus 3.13+ is mid-transition to a new
SimData/SimDevice datastore API and its classic ModbusDeviceContext API
prints deprecation warnings; the in-house fixture has none of that churn
and is exactly what the automated tests already exercise.)

Usage:
    python tools/mock_modbus_server.py [--port 5020]

Holding registers:
    0: 2731   (-> 0.0 degC via the example config's scale=0.1, offset=-273.15)
    1: 1013   (pressure_01, unscaled)
Coils:
    0: True   (pump_running)
Discrete inputs:
    0: False  (door_open)

The temperature register drifts by a random amount every few seconds so you
can see live-changing values (and exercise deadband suppression).
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fixtures.fake_modbus_server import FakeModbusServer  # noqa: E402


async def _drift_holding_register_0(server: FakeModbusServer) -> None:
    while True:
        await asyncio.sleep(3)
        server.holding_registers[0] = 2731 + random.randint(-20, 20)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5020)
    args = parser.parse_args()

    server = FakeModbusServer()
    server.holding_registers = {0: 2731, 1: 1013}
    server.input_registers = {0: 1013}
    server.coils = {0: True}
    server.discrete_inputs = {0: False}

    # FakeModbusServer.start() always binds an OS-assigned port; bind the
    # requested fixed port directly instead so the example config's
    # `config.port` stays predictable across runs.
    server._server = await asyncio.start_server(  # noqa: SLF001
        server._handle_client,  # noqa: SLF001
        server.host,
        args.port,
    )

    print(f"Modbus TCP simulator listening on {server.host}:{args.port}")
    print("Point config/examples/modbus-tcp-example.yaml's config.host/port at this.")

    drift_task = asyncio.ensure_future(_drift_holding_register_0(server))
    try:
        async with server._server:  # noqa: SLF001
            await server._server.serve_forever()  # noqa: SLF001
    finally:
        drift_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
