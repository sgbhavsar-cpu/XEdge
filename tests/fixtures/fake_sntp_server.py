"""Fake SNTP (RFC 4330) server for testing xedge.core.sntp without a real
NTP server or any network access — same rationale as fake_modbus_server.py.
"""

from __future__ import annotations

import asyncio
import struct
import time

_NTP_UNIX_EPOCH_DELTA = 2_208_988_800


def _to_ntp_timestamp(unix_time: float) -> bytes:
    ntp_time = unix_time + _NTP_UNIX_EPOCH_DELTA
    seconds = int(ntp_time)
    fraction = int((ntp_time - seconds) * (1 << 32))
    return struct.pack(">II", seconds, fraction)


class _FakeSntpServerProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: FakeSntpServer) -> None:
        self._server = server
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._server.request_count += 1
        if self._server.drop_requests:
            return
        assert self._transport is not None
        self._transport.sendto(self._server.build_response(data), addr)


class FakeSntpServer:
    """In-memory SNTP server. `offset_seconds` shifts the server's reported
    time relative to real wall-clock time, so a test can assert on a
    specific, known offset instead of depending on real clock skew (which
    on a test machine is normally ~0, making the two hard to tell apart)."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 0
        self.request_count = 0
        self.drop_requests = False
        self.stratum = 1
        self.offset_seconds = 0.0
        self.response_override: bytes | None = None
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _FakeSntpServerProtocol(self), local_addr=(self.host, 0)
        )
        self._transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def build_response(self, request: bytes) -> bytes:
        if self.response_override is not None:
            return self.response_override
        server_now = time.time() + self.offset_seconds
        packet = bytearray(48)
        packet[0] = 0b00_100_100  # LI=0, VN=4, Mode=4 (server)
        packet[1] = self.stratum
        packet[24:32] = request[40:48]  # echo client's Transmit Timestamp as Originate
        packet[32:40] = _to_ntp_timestamp(server_now)  # Receive Timestamp
        packet[40:48] = _to_ntp_timestamp(server_now)  # Transmit Timestamp
        return bytes(packet)
