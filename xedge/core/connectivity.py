"""Shared connectivity state machine (Sprint C2, XEDGE-420; ADR-011 Part 3).

XEDGE-CRD-001 describes near-identical connectivity models in three places:
Modbus device health (§4.4), Gateway connection state (§4.9), and Asset
connection state (§4.11). Building each separately guarantees drift —
different thresholds, different hysteresis, three separate bugs when a
flapping link produces state churn. This is the one implementation all three
contexts adapt to their own vocabulary.

Deliberately dependency-free (stdlib only) so it can be imported from
anywhere — drivers, fleet, and the future asset layer — without creating an
import cycle back into any of them.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime


class ConnectivityState(enum.Enum):
    """`UNKNOWN` is the only state a fresh tracker starts in — it means "no
    result observed yet," distinct from `NOT_CONNECTED` ("results observed,
    device is failing"). Contexts with fewer states (e.g. the CRD's `Not
    Connected` alone) collapse this set down; see the per-context adapters
    that consume this module (e.g. `xedge.drivers.modbus.polling`)."""

    UNKNOWN = "unknown"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    NOT_CONNECTED = "not_connected"


DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_RECOVERY_THRESHOLD = 3


class ConnectivityTracker:
    """Consecutive-result state machine with asymmetric hysteresis.

    Two thresholds, not one: a single shared threshold makes a flapping link
    oscillate between Connected and Not Connected on every other request,
    which is exactly the alarm/audit-log churn a real device intermittently
    timing out would produce. Recovery is deliberately harder than failure —
    `recovery_threshold` consecutive successes are required to leave
    `NOT_CONNECTED`, so one lucky response after an outage doesn't flip the
    state straight back to green.

    Transitions:
        UNKNOWN        --success-->  CONNECTED
        UNKNOWN        --failure-->  DEGRADED (or NOT_CONNECTED if
                                      failure_threshold <= 1)
        CONNECTED      --success-->  CONNECTED (failure streak reset)
        CONNECTED      --failure-->  DEGRADED, or straight to NOT_CONNECTED
                                      if failure_threshold <= 1
        DEGRADED       --success-->  CONNECTED immediately — a single
                                      success while merely degraded (not yet
                                      past failure_threshold) recovers at
                                      once; only a confirmed NOT_CONNECTED
                                      needs the harder recovery_threshold
        DEGRADED       --failure-->  NOT_CONNECTED once failure_threshold
                                      consecutive failures are reached,
                                      otherwise stays DEGRADED
        NOT_CONNECTED  --success-->  stays NOT_CONNECTED until
                                      recovery_threshold consecutive
                                      successes accumulate, then CONNECTED
        NOT_CONNECTED  --failure-->  stays NOT_CONNECTED; any partial
                                      recovery streak is reset
    """

    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_threshold: int = DEFAULT_RECOVERY_THRESHOLD,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")
        if recovery_threshold < 1:
            raise ValueError(f"recovery_threshold must be >= 1, got {recovery_threshold}")
        self._failure_threshold = failure_threshold
        self._recovery_threshold = recovery_threshold
        self._state = ConnectivityState.UNKNOWN
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_failure_reason: str | None = None
        self._changed_at = datetime.now(UTC)

    @property
    def state(self) -> ConnectivityState:
        return self._state

    @property
    def changed_at(self) -> datetime:
        """When `state` last actually changed — not updated on a
        record_success()/record_failure() call that leaves the state as it
        was. Lets a caller show "Not Connected for 4 minutes," not just
        "last checked 2 seconds ago.\""""
        return self._changed_at

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def consecutive_successes(self) -> int:
        return self._consecutive_successes

    @property
    def last_failure_reason(self) -> str | None:
        return self._last_failure_reason

    def record_success(self, now: datetime | None = None) -> ConnectivityState:
        self._consecutive_failures = 0
        self._consecutive_successes += 1

        if self._state in (ConnectivityState.UNKNOWN, ConnectivityState.DEGRADED):
            self._transition(ConnectivityState.CONNECTED, now)
        elif self._state is ConnectivityState.NOT_CONNECTED:
            if self._consecutive_successes >= self._recovery_threshold:
                self._transition(ConnectivityState.CONNECTED, now)
        # else: already CONNECTED, nothing to do beyond the counter reset above.
        return self._state

    def record_failure(
        self, now: datetime | None = None, reason: str | None = None
    ) -> ConnectivityState:
        self._consecutive_successes = 0
        self._consecutive_failures += 1
        self._last_failure_reason = reason

        if self._state is ConnectivityState.NOT_CONNECTED:
            return self._state  # already the terminal failure state

        if self._consecutive_failures >= self._failure_threshold:
            self._transition(ConnectivityState.NOT_CONNECTED, now)
        else:
            self._transition(ConnectivityState.DEGRADED, now)
        return self._state

    def _transition(self, new_state: ConnectivityState, now: datetime | None) -> None:
        if new_state is self._state:
            return
        self._state = new_state
        self._changed_at = now if now is not None else datetime.now(UTC)
