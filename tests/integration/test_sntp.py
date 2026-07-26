"""Sprint C3 SNTP client tests (XEDGE-437; CRD §4.8): the wire-level query
against a fake local server (real UDP I/O, so the offset/delay math is an
assertion rather than a claim — same rationale test_modbus_batching.py
gives for using a fake TCP server), and the multi-server sync loop's
fallback/status-reporting behavior.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.fake_sntp_server import FakeSntpServer
from xedge.core.sntp import (
    SntpConfig,
    SntpProtocolError,
    SntpSyncStatus,
    query_sntp,
    sntp_sync_loop,
)


@pytest.fixture
async def fake_server() -> AsyncIterator[FakeSntpServer]:
    server = FakeSntpServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _unused_udp_port() -> int:
    """A real, currently-free UDP port that nothing listens on — used to
    exercise "server doesn't answer" without mocking anything."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _wait_until(
    predicate: Callable[[], bool], *, attempts: int = 300, interval: float = 0.01
) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


class TestQuerySntp:
    async def test_reports_the_configured_offset(self, fake_server: FakeSntpServer) -> None:
        fake_server.offset_seconds = 5.0
        fake_server.stratum = 2

        result = await query_sntp(fake_server.host, port=fake_server.port, timeout_seconds=2.0)

        # Loopback round trip is sub-millisecond; a wide-but-real tolerance
        # keeps this from being flaky on a loaded CI runner while still
        # failing outright if the offset math were, say, inverted or halved.
        assert result.offset_seconds == pytest.approx(5.0, abs=0.5)
        assert result.round_trip_delay_seconds >= 0
        assert result.stratum == 2
        assert result.server == fake_server.host

    async def test_received_at_is_utc_aware_and_current(self, fake_server: FakeSntpServer) -> None:
        result = await query_sntp(fake_server.host, port=fake_server.port, timeout_seconds=2.0)

        assert result.received_at.tzinfo is UTC
        assert abs((datetime.now(UTC) - result.received_at).total_seconds()) < 5

    async def test_times_out_when_server_does_not_answer(self, fake_server: FakeSntpServer) -> None:
        fake_server.drop_requests = True

        with pytest.raises(TimeoutError):
            await query_sntp(fake_server.host, port=fake_server.port, timeout_seconds=0.2)

    async def test_fails_against_a_port_nothing_listens_on(self) -> None:
        """Loopback UDP to a closed port typically bounces back an ICMP
        port-unreachable almost immediately (raised here as `OSError`), not
        a timeout — `sntp_sync_loop` treats both identically (this server
        didn't work this cycle), so either is an acceptable outcome."""
        with pytest.raises((TimeoutError, OSError)):
            await query_sntp("127.0.0.1", port=_unused_udp_port(), timeout_seconds=0.2)

    async def test_rejects_a_kiss_of_death_stratum_zero_response(
        self, fake_server: FakeSntpServer
    ) -> None:
        fake_server.stratum = 0

        with pytest.raises(SntpProtocolError):
            await query_sntp(fake_server.host, port=fake_server.port, timeout_seconds=2.0)

    async def test_rejects_a_too_short_response(self, fake_server: FakeSntpServer) -> None:
        fake_server.response_override = b"\x00" * 10

        with pytest.raises(SntpProtocolError):
            await query_sntp(fake_server.host, port=fake_server.port, timeout_seconds=2.0)


class TestSntpSyncLoop:
    async def test_syncs_from_the_first_server_that_answers(
        self, fake_server: FakeSntpServer
    ) -> None:
        config = SntpConfig(
            servers=[f"{fake_server.host}:{fake_server.port}"], sync_interval_seconds=10
        )
        status = SntpSyncStatus()
        task = asyncio.create_task(sntp_sync_loop(config, status))
        try:
            await _wait_until(lambda: status.last_sync_at is not None)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert status.enabled is True
        assert status.last_sync_server == f"{fake_server.host}:{fake_server.port}"
        assert status.offset_seconds is not None
        assert status.consecutive_failures == 0
        assert status.last_error is None

    async def test_falls_back_to_the_next_server_when_the_first_is_unreachable(
        self, fake_server: FakeSntpServer
    ) -> None:
        """`host:port` shorthand lets a test point two different "servers"
        at two different local ports — one dead, one the fake server — and
        exercise real, unmocked fallback-on-failure."""
        dead_server = f"127.0.0.1:{_unused_udp_port()}"
        working_server = f"{fake_server.host}:{fake_server.port}"
        config = SntpConfig(
            servers=[dead_server, working_server],
            sync_interval_seconds=10,
            timeout_seconds=0.3,
        )
        status = SntpSyncStatus()
        task = asyncio.create_task(sntp_sync_loop(config, status))
        try:
            await _wait_until(lambda: status.last_sync_at is not None, attempts=500)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert status.last_sync_server == working_server
        assert status.consecutive_failures == 0

    async def test_all_servers_failing_increments_failures_without_erasing_history(
        self, fake_server: FakeSntpServer
    ) -> None:
        config = SntpConfig(
            servers=[f"{fake_server.host}:{fake_server.port}"],
            sync_interval_seconds=0.05,
            timeout_seconds=0.2,
        )
        status = SntpSyncStatus()
        task = asyncio.create_task(sntp_sync_loop(config, status))
        try:
            await _wait_until(lambda: status.last_sync_at is not None)
            first_sync_at = status.last_sync_at
            assert first_sync_at is not None

            fake_server.drop_requests = True
            await _wait_until(lambda: status.consecutive_failures >= 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert status.consecutive_failures >= 2
        assert status.last_error is not None
        # The last successful sync's data is still there, not cleared by
        # the subsequent failures.
        assert status.last_sync_at == first_sync_at
        assert status.offset_seconds is not None


class TestSntpSyncStatusStaleness:
    def test_stale_before_any_sync(self) -> None:
        assert SntpSyncStatus().is_stale is True

    def test_not_stale_shortly_after_a_sync(self) -> None:
        status = SntpSyncStatus(last_sync_at=datetime.now(UTC), stale_after_seconds=3600)
        assert status.is_stale is False

    def test_stale_once_older_than_the_configured_threshold(self) -> None:
        status = SntpSyncStatus(
            last_sync_at=datetime.now(UTC) - timedelta(seconds=100), stale_after_seconds=10
        )
        assert status.is_stale is True
