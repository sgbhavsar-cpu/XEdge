"""Alarm engine v2 (Sprint 31, XEDGE-224/225/228): threshold and rate-of-
change alarm detection, an ack/shelve state machine, and alarm-tier data
retention independent of ordinary telemetry.

Runs as a pipeline stage (`xedge.core.main._pipeline_to_buffer`) after
`xedge.core.pipeline.normalize`, before deadband filtering — an alarm
state *transition* always survives deadband suppression (see
`xedge.core.pipeline.DeadbandFilter`'s `is_alarm`-aware force-publish),
the same way a quality change already does.

"Alarm-tier" is a *static* property of a tag (does it have an `AlarmRule`
configured at all — `has_rule()`), independent of whether it is
*currently* alarming (`AlarmStatus.state`, dynamic). A monitored point
that hasn't tripped in the last hour still deserves backpressure-not-
eviction and longer retention — see `xedge.store.ring_buffer`'s own
`is_alarm_stream` docstring, written in anticipation of this module
("Alarm streams (none exist until the Sprint 31 alarm engine)...").
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import TagValue

# Ring-buffer/cold-store stream-key suffix identifying a tag's stream as
# alarm-tier (backpressure instead of eviction, independently retained) —
# see xedge.core.main._pipeline_to_buffer (writer) and
# xedge.store.sqlite_store.purge_loop (reader) for the two call sites that
# need to agree on this naming convention.
ALARM_STREAM_KEY_SUFFIX = "::alarm"


@dataclass(frozen=True, slots=True)
class AlarmRule:
    tag_id: str
    high: float | None = None
    high_high: float | None = None
    low: float | None = None
    low_low: float | None = None
    # Absolute value change per second beyond which a rate-of-change alarm
    # trips — evaluated on consecutive samples of the *same* tag_id, so the
    # first sample after a gap (or the very first sample ever) never trips
    # this condition (no prior sample to compare against).
    rate_of_change_per_second: float | None = None


class AlarmState(enum.Enum):
    NORMAL = "normal"
    ACTIVE = "active"
    ACTIVE_ACKED = "active_acked"


@dataclass(slots=True)
class AlarmStatus:
    tag_id: str
    state: AlarmState = AlarmState.NORMAL
    condition: str | None = None
    active_since: datetime | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    shelved_until: datetime | None = None
    last_value: TagValue | None = None
    last_rate_per_second: float | None = None


def _is_numeric(value: TagValue) -> bool:
    # bool is an int subclass in Python but isn't a meaningful alarm target.
    return isinstance(value, int | float) and not isinstance(value, bool)


class AlarmEngine:
    def __init__(self, rules: dict[str, AlarmRule]) -> None:
        self._rules = rules
        self._status: dict[str, AlarmStatus] = {}
        self._last_sample: dict[str, tuple[float, datetime]] = {}

    def has_rule(self, tag_id: str) -> bool:
        return tag_id in self._rules

    def evaluate(self, tag: UnifiedTag) -> UnifiedTag:
        """Evaluate `tag` against its configured rule (if any), updating
        this engine's internal state, and return a copy of `tag` with
        `is_alarm` set — `True` only while `ACTIVE`/`ACTIVE_ACKED` *and*
        not currently shelved. A tag with no rule is returned unchanged."""
        rule = self._rules.get(tag.tag_id)
        if rule is None:
            return tag

        status = self._status.setdefault(tag.tag_id, AlarmStatus(tag_id=tag.tag_id))
        status.last_value = tag.value
        condition = self._check_conditions(rule, tag, status)

        if condition is not None:
            if status.state == AlarmState.NORMAL:
                status.state = AlarmState.ACTIVE
                status.active_since = tag.timestamp
                status.acknowledged_by = None
                status.acknowledged_at = None
            status.condition = condition
        else:
            status.state = AlarmState.NORMAL
            status.condition = None
            status.active_since = None

        shelved = status.shelved_until is not None and tag.timestamp < status.shelved_until
        is_alarm = status.state != AlarmState.NORMAL and not shelved
        return replace(tag, is_alarm=is_alarm)

    def _check_conditions(
        self, rule: AlarmRule, tag: UnifiedTag, status: AlarmStatus
    ) -> str | None:
        if rule.rate_of_change_per_second is not None:
            rate_condition = self._check_rate_of_change(rule, tag, status)
        else:
            rate_condition = None

        if not _is_numeric(tag.value):
            return rate_condition
        value = float(tag.value)
        # Checked worst-first: a value that's both "high" and "high_high"
        # (or "low"/"low_low") reports the more severe condition.
        if rule.high_high is not None and value >= rule.high_high:
            return "high_high"
        if rule.low_low is not None and value <= rule.low_low:
            return "low_low"
        if rule.high is not None and value >= rule.high:
            return "high"
        if rule.low is not None and value <= rule.low:
            return "low"
        return rate_condition

    def _check_rate_of_change(
        self, rule: AlarmRule, tag: UnifiedTag, status: AlarmStatus
    ) -> str | None:
        if not _is_numeric(tag.value):
            return None
        value = float(tag.value)
        prior = self._last_sample.get(tag.tag_id)
        self._last_sample[tag.tag_id] = (value, tag.timestamp)
        if prior is None:
            return None
        prior_value, prior_timestamp = prior
        elapsed_seconds = (tag.timestamp - prior_timestamp).total_seconds()
        if elapsed_seconds <= 0:
            return None
        rate = abs(value - prior_value) / elapsed_seconds
        status.last_rate_per_second = rate
        if rule.rate_of_change_per_second is not None and rate > rule.rate_of_change_per_second:
            return "rate_of_change"
        return None

    def acknowledge(self, tag_id: str, actor: str) -> bool:
        """Returns False if `tag_id` isn't currently ACTIVE (unknown tag,
        already acked, or not alarming) — nothing to acknowledge."""
        status = self._status.get(tag_id)
        if status is None or status.state != AlarmState.ACTIVE:
            return False
        status.state = AlarmState.ACTIVE_ACKED
        status.acknowledged_by = actor
        status.acknowledged_at = datetime.now(UTC)
        return True

    def shelve(self, tag_id: str, duration_seconds: float) -> None:
        """Suppress `is_alarm` for `tag_id` for `duration_seconds` —
        detection/state tracking continues underneath (an operator
        silencing a known, already-being-worked issue), it just stops
        being reported as active until the shelve period lapses or
        `unshelve` is called explicitly."""
        status = self._status.setdefault(tag_id, AlarmStatus(tag_id=tag_id))
        status.shelved_until = datetime.now(UTC) + timedelta(seconds=duration_seconds)

    def unshelve(self, tag_id: str) -> bool:
        status = self._status.get(tag_id)
        if status is None or status.shelved_until is None:
            return False
        status.shelved_until = None
        return True

    def all_status(self) -> dict[str, AlarmStatus]:
        return dict(self._status)


def build_alarm_rules(rules_config: list[dict[str, Any]]) -> dict[str, AlarmRule]:
    """Build a `tag_id -> AlarmRule` lookup from the `alarms.rules` config
    section (a flat list, since alarms are cross-cutting and not scoped to
    any one driver type — unlike deadband/scaling, which live under each
    driver's own tag_groups)."""
    rules: dict[str, AlarmRule] = {}
    for entry in rules_config:
        tag_id: str = entry["tag_id"]
        rules[tag_id] = AlarmRule(
            tag_id=tag_id,
            high=entry.get("high"),
            high_high=entry.get("high_high"),
            low=entry.get("low"),
            low_low=entry.get("low_low"),
            rate_of_change_per_second=entry.get("rate_of_change_per_second"),
        )
    return rules
