"""In-house SNTP client (RFC 4330) — CRD §4.8: multi-server, configurable
sync interval, sync-status reporting. Implemented directly over a UDP
datagram (a single 48-byte request/response exchange is the entire wire
protocol) rather than adding a dependency for something this small — the
same rationale `xedge.core.watchdog` gives for its own sd_notify socket
code.

This module only *queries* time; it does not set the system clock. HLR's
ASM-001 already assumes the host OS runs an NTP-synchronized clock, and
xEdge's own timestamping (FR-DP-001) reads that OS clock — so the
underlying data model doesn't change. The gap this closes is *visibility*:
today there is no way to see, from xEdge itself, whether that assumption
is actually true — which server answered, how large the offset is, or
whether sync has gone stale — which is what `SntpSyncStatus` below reports
to the REST API and Web UI.
"""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_NTP_PORT = 123
_NTP_PACKET_LENGTH = 48
# Seconds between the NTP epoch (1900-01-01T00:00:00Z) and the Unix epoch
# (1970-01-01T00:00:00Z) — every NTP timestamp field is relative to the
# former; Python's is relative to the latter.
_NTP_UNIX_EPOCH_DELTA = 2_208_988_800
# LI=0 (no warning), VN=4, Mode=3 (client) — the one request byte 0 needs.
_CLIENT_REQUEST_FIRST_BYTE = 0b00_100_011


class SntpProtocolError(Exception):
    """A server answered, but not with a usable SNTP response (too short,
    or a Kiss-o'-Death stratum-0 reply asking the client to back off)."""


@dataclass(slots=True)
class SntpQueryResult:
    server: str
    offset_seconds: float
    round_trip_delay_seconds: float
    stratum: int
    received_at: datetime


@dataclass(slots=True)
class SntpConfig:
    """`servers` entries are `"host"` (standard port 123) or `"host:port"`
    (see `_parse_server`) — tried in this order every cycle, stopping at
    the first that answers (XEDGE-437's multi-server requirement)."""

    servers: list[str]
    sync_interval_seconds: float = 3600
    timeout_seconds: float = 5.0
    timezone: str = "UTC"
    stale_after_seconds: float = 7200


@dataclass(slots=True)
class SntpSyncStatus:
    """Live snapshot the Web UI/REST API read (`GET /api/v1/sntp/status`) —
    mutated in place by `sntp_sync_loop`, a single shared instance rather
    than a queue/pubsub, the same shape `xedge.fleet.agent.FleetAgentStatus`
    already uses for exactly this reason."""

    enabled: bool = False
    servers: list[str] = field(default_factory=list)
    sync_interval_seconds: float | None = None
    timezone: str = "UTC"
    stale_after_seconds: float = 7200
    last_sync_at: datetime | None = None
    last_sync_server: str | None = None
    offset_seconds: float | None = None
    round_trip_delay_seconds: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None

    @property
    def is_stale(self) -> bool:
        """No successful sync yet, or the last one is older than
        `stale_after_seconds` — independent of `consecutive_failures`, since
        a server that starts failing only *after* a long-since-stale
        success should still read as stale, not merely "failing"."""
        if self.last_sync_at is None:
            return True
        age_seconds = (datetime.now(UTC) - self.last_sync_at).total_seconds()
        return age_seconds > self.stale_after_seconds


def _to_ntp_timestamp(unix_time: float) -> bytes:
    ntp_time = unix_time + _NTP_UNIX_EPOCH_DELTA
    seconds = int(ntp_time)
    fraction = int((ntp_time - seconds) * (1 << 32))
    return struct.pack(">II", seconds, fraction)


def _from_ntp_timestamp(data: bytes) -> float:
    seconds, fraction = struct.unpack(">II", data)
    return (int(seconds) - _NTP_UNIX_EPOCH_DELTA) + int(fraction) / (1 << 32)


def _parse_server(server: str) -> tuple[str, int]:
    """`"host"` (implies the standard NTP port 123) or `"host:port"`, for
    the rare deployment pointing at an NTP server on a non-standard port
    (e.g. an isolated OT network running its own time source instead of
    the host's own systemd-timesyncd/chrony). Splits on the *last* colon,
    so a bare IPv6 literal — which itself contains colons — isn't a valid
    input to this shorthand; a hostname or IPv4 literal covers every stated
    requirement."""
    host, sep, port_str = server.rpartition(":")
    if not sep:
        return server, _NTP_PORT
    return host, int(port_str)


class _SntpClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_response: asyncio.Future[tuple[bytes, float]]) -> None:
        self._on_response = on_response

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self._on_response.done():
            self._on_response.set_result((data, time.time()))

    def error_received(self, exc: Exception) -> None:
        if not self._on_response.done():
            self._on_response.set_exception(exc)


async def query_sntp(
    server: str, *, port: int = _NTP_PORT, timeout_seconds: float = 5.0
) -> SntpQueryResult:
    """One RFC 4330 SNTP request/response exchange against `server`.

    Computes the standard 4-timestamp offset/delay (T1 originate, T2 server
    receive, T3 server transmit, T4 destination) rather than the 2-timestamp
    shortcut some minimal SNTP clients use — the round trip is already paid
    for, and the extra accuracy costs nothing more.

    Raises `TimeoutError` if nothing answers within `timeout_seconds`,
    `OSError` for a network/DNS failure, or `SntpProtocolError` for a
    malformed or Kiss-o'-Death response. `sntp_sync_loop` treats all three
    identically: this server didn't work this cycle, try the next one.
    """
    loop = asyncio.get_running_loop()
    on_response: asyncio.Future[tuple[bytes, float]] = loop.create_future()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: _SntpClientProtocol(on_response), remote_addr=(server, port)
    )
    try:
        originate_unix_time = time.time()
        request = bytearray(_NTP_PACKET_LENGTH)
        request[0] = _CLIENT_REQUEST_FIRST_BYTE
        request[40:48] = _to_ntp_timestamp(originate_unix_time)
        transport.sendto(bytes(request))

        data, t4 = await asyncio.wait_for(on_response, timeout=timeout_seconds)
    finally:
        transport.close()

    if len(data) < _NTP_PACKET_LENGTH:
        raise SntpProtocolError(f"response too short ({len(data)} bytes)")

    stratum = data[1]
    if stratum == 0:
        raise SntpProtocolError("server sent a Kiss-o'-Death (stratum 0) response")

    t1 = _from_ntp_timestamp(bytes(data[24:32]))  # Originate Timestamp, echoed back
    t2 = _from_ntp_timestamp(bytes(data[32:40]))  # Receive Timestamp
    t3 = _from_ntp_timestamp(bytes(data[40:48]))  # Transmit Timestamp

    offset = ((t2 - t1) + (t3 - t4)) / 2
    # RFC 4330's delay formula mixes the local clock (t1, t4) with the
    # server's own reported clock (t2, t3) -- it cannot be negative in
    # reality, but on a very fast round trip (a loopback/local test server,
    # sub-millisecond) clock-read granularity noise can push the raw
    # subtraction fractionally below zero. Clamp rather than propagate a
    # physically nonsensical negative round-trip time to callers/the UI.
    delay = max(0.0, (t4 - t1) - (t3 - t2))
    return SntpQueryResult(
        server=server,
        offset_seconds=offset,
        round_trip_delay_seconds=delay,
        stratum=stratum,
        received_at=datetime.fromtimestamp(t4, tz=UTC),
    )


async def sntp_sync_loop(config: SntpConfig, status: SntpSyncStatus) -> None:
    """Runs as one more asyncio task inside `xedge.core.main.async_main`,
    the same shape as `xedge.fleet.agent.fleet_heartbeat_loop`: tries each
    of `config.servers` in order every `sync_interval_seconds`, stopping at
    the first that answers. A cycle where every server fails does not
    clear `last_sync_at`/`offset_seconds` — the whole point of reporting
    staleness (`SntpSyncStatus.is_stale`) is comparing "now" against the
    last time a sync actually worked, which a reset-on-failure would erase.
    """
    status.enabled = True
    status.servers = list(config.servers)
    status.sync_interval_seconds = config.sync_interval_seconds
    status.timezone = config.timezone
    status.stale_after_seconds = config.stale_after_seconds

    while True:
        synced = False
        last_error: str | None = None
        for server in config.servers:
            host, port = _parse_server(server)
            try:
                result = await query_sntp(host, port=port, timeout_seconds=config.timeout_seconds)
            except (OSError, TimeoutError, SntpProtocolError) as exc:
                last_error = f"{server}: {exc}"
                continue
            status.last_sync_at = result.received_at
            status.last_sync_server = server
            status.offset_seconds = result.offset_seconds
            status.round_trip_delay_seconds = result.round_trip_delay_seconds
            status.consecutive_failures = 0
            status.last_error = None
            logger.info(
                "sntp.synced",
                server=server,
                offset_seconds=round(result.offset_seconds, 6),
                stratum=result.stratum,
            )
            synced = True
            break

        if not synced:
            status.consecutive_failures += 1
            status.last_error = last_error
            logger.warning(
                "sntp.sync_failed",
                servers=config.servers,
                consecutive_failures=status.consecutive_failures,
                error=last_error,
            )

        await asyncio.sleep(config.sync_interval_seconds)
