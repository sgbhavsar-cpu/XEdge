"""Unit tests for xedge.fleet.agent's pure logic (ADR-013 §3, XEDGE-443's
rotation carried into Sprint C5). `_cert_needs_rotation` decides whether
every device in the fleet re-keys on a given heartbeat -- a wrong
comparison direction here means either "never rotates" (silently repeats
the exact gap this feature exists to close) or "rotates on every single
heartbeat" (hammers the Fleet Manager with re-key requests for every
enrolled device). Cheap and fast enough to pin down directly, separate
from tests/integration/test_fleet_agent.py's real-mTLS end-to-end proof
that a rotation, once triggered, actually works.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xedge.fleet.agent import _cert_needs_rotation


def _in_days(days: float) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


class TestCertNeedsRotation:
    def test_false_with_most_of_the_validity_period_remaining(self) -> None:
        assert _cert_needs_rotation(_in_days(90), threshold_days=30) is False

    def test_true_once_fewer_days_remain_than_the_threshold(self) -> None:
        assert _cert_needs_rotation(_in_days(10), threshold_days=30) is True

    def test_true_for_a_certificate_that_has_already_expired(self) -> None:
        assert _cert_needs_rotation(_in_days(-1), threshold_days=30) is True

    def test_false_comfortably_above_the_threshold(self) -> None:
        assert _cert_needs_rotation(_in_days(31), threshold_days=30) is False
