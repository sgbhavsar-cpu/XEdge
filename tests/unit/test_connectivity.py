"""Connectivity state machine tests (Sprint C2, XEDGE-420).

This is the state machine device health, gateway connection state, and asset
connection state all adapt to their own vocabulary (ADR-011 Part 3) — its
transitions are the one thing all three inherit, so its hysteresis behaviour
is worth testing thoroughly rather than just its happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xedge.core.connectivity import ConnectivityState, ConnectivityTracker


class TestInitialState:
    def test_starts_unknown(self) -> None:
        assert ConnectivityTracker().state is ConnectivityState.UNKNOWN

    def test_rejects_a_threshold_below_one(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            ConnectivityTracker(failure_threshold=0)
        with pytest.raises(ValueError, match="recovery_threshold"):
            ConnectivityTracker(recovery_threshold=0)


class TestFromUnknown:
    def test_single_success_connects(self) -> None:
        tracker = ConnectivityTracker()
        assert tracker.record_success() is ConnectivityState.CONNECTED

    def test_single_failure_degrades_when_threshold_above_one(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=3)
        assert tracker.record_failure() is ConnectivityState.DEGRADED

    def test_single_failure_goes_straight_to_not_connected_when_threshold_is_one(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=1)
        assert tracker.record_failure() is ConnectivityState.NOT_CONNECTED


class TestFailureThreshold:
    def test_stays_degraded_below_threshold(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=3)
        tracker.record_success()
        tracker.record_failure()
        assert tracker.record_failure() is ConnectivityState.DEGRADED

    def test_reaches_not_connected_at_threshold(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=3)
        tracker.record_success()
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.record_failure() is ConnectivityState.NOT_CONNECTED

    def test_further_failures_stay_not_connected(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=2)
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.state is ConnectivityState.NOT_CONNECTED
        assert tracker.record_failure() is ConnectivityState.NOT_CONNECTED
        assert tracker.consecutive_failures == 3


class TestRecoveryAsymmetry:
    """The load-bearing property: recovering from a confirmed outage is
    harder than degrading into one, so a single lucky response doesn't
    flip the state straight back to green."""

    def test_a_single_success_from_degraded_recovers_immediately(self) -> None:
        """Not yet a confirmed outage — a momentary blip should clear fast."""
        tracker = ConnectivityTracker(failure_threshold=3, recovery_threshold=3)
        tracker.record_success()
        tracker.record_failure()
        assert tracker.state is ConnectivityState.DEGRADED
        assert tracker.record_success() is ConnectivityState.CONNECTED

    def test_a_single_success_from_not_connected_does_not_recover(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=2, recovery_threshold=3)
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.state is ConnectivityState.NOT_CONNECTED
        assert tracker.record_success() is ConnectivityState.NOT_CONNECTED

    def test_recovers_once_recovery_threshold_successes_accumulate(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=2, recovery_threshold=3)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_success()
        tracker.record_success()
        assert tracker.record_success() is ConnectivityState.CONNECTED

    def test_a_failure_during_recovery_resets_the_success_streak(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=2, recovery_threshold=3)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_success()
        tracker.record_success()
        tracker.record_failure()  # interrupts the recovery streak
        assert tracker.state is ConnectivityState.NOT_CONNECTED
        tracker.record_success()
        tracker.record_success()
        assert tracker.state is ConnectivityState.NOT_CONNECTED, (
            "streak must have reset to 2, not 3"
        )
        assert tracker.record_success() is ConnectivityState.CONNECTED

    def test_recovery_threshold_of_one_recovers_on_first_success(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=1, recovery_threshold=1)
        tracker.record_failure()
        assert tracker.state is ConnectivityState.NOT_CONNECTED
        assert tracker.record_success() is ConnectivityState.CONNECTED


class TestCountersResetOnOppositeResult:
    def test_success_resets_the_failure_streak(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=5)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_success()
        assert tracker.consecutive_failures == 0
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.state is ConnectivityState.DEGRADED, "the earlier streak must not carry over"

    def test_failure_resets_the_success_streak_counter(self) -> None:
        tracker = ConnectivityTracker(recovery_threshold=5)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_success()
        assert tracker.consecutive_successes == 1
        tracker.record_failure()
        assert tracker.consecutive_successes == 0


class TestChangedAt:
    def test_does_not_move_on_a_call_that_does_not_change_state(self) -> None:
        tracker = ConnectivityTracker()
        tracker.record_success()
        first = tracker.changed_at
        tracker.record_success()
        tracker.record_success()
        assert tracker.changed_at == first

    def test_moves_on_an_actual_transition(self) -> None:
        tracker = ConnectivityTracker(failure_threshold=1)
        tracker.record_success()
        first = tracker.changed_at
        second = datetime(2026, 1, 1, tzinfo=UTC)
        assert second != first
        tracker.record_failure(now=second)
        assert tracker.changed_at == second

    def test_defaults_to_the_real_clock_when_now_is_omitted(self) -> None:
        before = datetime.now(UTC)
        tracker = ConnectivityTracker(failure_threshold=1)
        tracker.record_failure()
        after = datetime.now(UTC)
        assert before <= tracker.changed_at <= after


class TestFailureReason:
    def test_last_failure_reason_is_recorded(self) -> None:
        tracker = ConnectivityTracker()
        tracker.record_failure(reason="ILLEGAL_DATA_ADDRESS")
        assert tracker.last_failure_reason == "ILLEGAL_DATA_ADDRESS"

    def test_success_does_not_clear_the_last_reason(self) -> None:
        """The last known failure reason stays visible even once recovered —
        useful for "last seen error" style reporting."""
        tracker = ConnectivityTracker(failure_threshold=1, recovery_threshold=1)
        tracker.record_failure(reason="TIMEOUT")
        tracker.record_success()
        assert tracker.last_failure_reason == "TIMEOUT"

    def test_defaults_to_none(self) -> None:
        assert ConnectivityTracker().last_failure_reason is None


class TestRealisticFlappingScenario:
    def test_intermittent_single_failures_never_reach_not_connected(self) -> None:
        """A link that fails once every few requests but never enough in a
        row should read as Degraded, not flap into Not Connected — that is
        the whole point of a *consecutive*-failure threshold."""
        tracker = ConnectivityTracker(failure_threshold=3, recovery_threshold=2)
        tracker.record_success()
        for _ in range(10):
            tracker.record_failure()
            state = tracker.record_success()
            assert state in (ConnectivityState.CONNECTED, ConnectivityState.DEGRADED)
        assert tracker.state is not ConnectivityState.NOT_CONNECTED
