"""Modbus RTU driver over a real serial line (RS-232/RS-485), FR-SA-001.

Sprint C3 (XEDGE-431) moved all actual wire I/O out of this class and into
`xedge.drivers.modbus.bus_manager.SerialBusManager`, shared by every
instance configured against the same `port` path. Before this, each
instance opened the serial port independently — the largest functional gap
in the customer's primary protocol, since two instances sharing a real
RS-485 multi-drop bus (`unit_id=1` and `unit_id=2` on the same
`/dev/ttyUSB0`) both tried to own the handle. This class is now a thin
shell: acquire the shared manager, delegate every read/write to it by
`unit_id`. See ADR-011 Part 1.

`_scheduler` is overridden to return the bus manager's *shared* scheduler —
see `polling.BaseModbusPollingDriver._scheduler`'s docstring for why a
shared bus needs one scheduler across every slave on it, not one per
instance.
"""

from __future__ import annotations

from typing import Any

from xedge.drivers.base import DriverConfig
from xedge.drivers.modbus import codec
from xedge.drivers.modbus.bus_manager import SerialBusManager, get_bus_manager
from xedge.drivers.modbus.polling import BaseModbusPollingDriver, ModbusDriverStateError
from xedge.drivers.modbus.scheduler import RequestScheduler
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_BYTES_PER_REGISTER = 2
# A Modbus RTU read request is 8 bytes on the wire (unit, function, address
# hi/lo, quantity hi/lo, CRC lo/hi). Its response is 5 bytes of overhead
# (unit, function, byte count, CRC lo/hi) plus 2 bytes per register.
_REQUEST_BYTES = 8
_RESPONSE_OVERHEAD_BYTES = 5
# T3.5 idle before each frame, per the Modbus over Serial Line spec.
_INTER_FRAME_CHARS = 3.5


def minimum_scan_interval_seconds(
    baud_rate: int,
    blocks: list[tuple[int, int]],
    data_bits: int = 8,
    parity: str = "none",
    stop_bits: int = 1,
) -> float:
    """Shortest cycle a serial bus can physically sustain for `blocks`.

    XEDGE-413 lowered the configurable scan-rate floor to 1 ms, which is
    reachable on TCP but **not** on RS-485: a serial line moves a fixed number
    of bits per second, so a cycle cannot outrun the time to clock its own
    frames out and back. This computes that bound so the operator gets told,
    rather than silently configuring a rate the hardware cannot deliver.

    `blocks` is (function_code, quantity) per planned read. Bit function codes
    pack 8 values per byte, register codes use 2 bytes each.

    Note (Sprint C3): this remains a *per-instance* estimate of what one
    slave's own reads would cost in isolation. On a shared multi-drop bus
    (`SerialBusManager`) the achievable rate for any one slave also depends
    on how many other slaves' requests interleave with its own — a bound
    this function cannot see, since it only knows one instance's config.
    """
    bits_per_char = 1 + data_bits + (0 if parity == "none" else 1) + stop_bits
    total_chars = 0.0
    for function_code, quantity in blocks:
        if codec.is_bit_function(codec.FunctionCode(function_code)):
            data_bytes = -(-quantity // 8)  # ceiling division: 8 coils per byte
        else:
            data_bytes = quantity * _BYTES_PER_REGISTER
        total_chars += _INTER_FRAME_CHARS + _REQUEST_BYTES + _RESPONSE_OVERHEAD_BYTES + data_bytes
    return total_chars * bits_per_char / baud_rate


__all__ = ["ModbusRtuSerialDriver", "minimum_scan_interval_seconds"]


class ModbusRtuSerialDriver(BaseModbusPollingDriver):
    def __init__(self) -> None:
        super().__init__()
        self._bus_manager: SerialBusManager | None = None
        self._unit_id = 1

    @property
    def _scheduler(self) -> RequestScheduler:
        if self._bus_manager is None:
            raise ModbusDriverStateError("connect() must be called before this operation")
        return self._bus_manager.scheduler

    async def configure(self, config: DriverConfig) -> None:
        await super().configure(config)
        self._unit_id = config.config.get("unit_id", 1)
        self._warn_on_unachievable_scan_rates(config)

    def _warn_on_unachievable_scan_rates(self, config: DriverConfig) -> None:
        """XEDGE-413 / open item Q-2. The schema floor is 1 ms for every
        transport, but RS-485 cannot deliver that — the bus moves a fixed
        number of bits per second. Rather than let a group silently overrun
        every single cycle, compute the physical bound at configure() time and
        say so once, naming the rate that would actually work.
        """
        cfg: dict[str, Any] = config.config
        baud_rate = cfg.get("baud_rate", 9600)
        for group in config.tag_groups:
            blocks = self._blocks_by_group.get(group["id"], [])
            if not blocks:
                continue
            floor_seconds = minimum_scan_interval_seconds(
                baud_rate,
                [(int(block.function_code), block.quantity) for block in blocks],
                data_bits=cfg.get("data_bits", 8),
                parity=cfg.get("parity", "none"),
                stop_bits=cfg.get("stop_bits", 1),
            )
            configured_seconds = group["scan_rate_ms"] / 1000
            if configured_seconds < floor_seconds:
                logger.warning(
                    "modbus.scan_rate_below_serial_floor",
                    instance_id=config.instance_id,
                    tag_group=group["id"],
                    configured_scan_rate_ms=group["scan_rate_ms"],
                    achievable_scan_rate_ms=round(floor_seconds * 1000, 1),
                    baud_rate=baud_rate,
                    request_count=len(blocks),
                )

    async def _connect_transport(self) -> None:
        cfg = self._require_config().config
        self._bus_manager = get_bus_manager(cfg["port"])
        await self._bus_manager.acquire(cfg)

    async def _disconnect_transport(self) -> None:
        if self._bus_manager is not None:
            await self._bus_manager.release()
            self._bus_manager = None

    def _require_bus_manager(self) -> SerialBusManager:
        if self._bus_manager is None:
            raise ModbusDriverStateError("connect() must be called before this operation")
        return self._bus_manager

    async def _read_block(
        self, function_code: codec.FunctionCode, address: int, quantity: int
    ) -> list[int] | list[bool]:
        return await self._require_bus_manager().read_block(
            self._unit_id, function_code, address, quantity
        )

    async def _write_one(
        self, function_code: codec.FunctionCode, address: int, value: int | bool
    ) -> None:
        await self._require_bus_manager().write_one(self._unit_id, function_code, address, value)

    async def _write_registers(self, address: int, values: list[int]) -> None:
        """FC16 (Sprint C2, XEDGE-423) — a tag whose data_type spans more
        than one register."""
        await self._require_bus_manager().write_registers(self._unit_id, address, values)
