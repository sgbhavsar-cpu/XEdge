from __future__ import annotations

from datetime import UTC, datetime

from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality
from xedge.store.latest_values import LatestValueStore


def _tag(tag_id: str, value: int, source_driver: str = "d1") -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,
        data_type="INT64",
        quality=Quality.GOOD,
        source_driver=source_driver,
        source_address="0",
    )


def test_for_driver_filters_by_instance_id_prefix() -> None:
    store = LatestValueStore()
    store.update(_tag("d1/t1", 1))
    store.update(_tag("d1/t2", 2))
    store.update(_tag("d2/t1", 3))

    d1_tags = {t.tag_id: t.value for t in store.for_driver("d1")}
    assert d1_tags == {"d1/t1": 1, "d1/t2": 2}


def test_update_overwrites_prior_value_for_same_tag_id() -> None:
    store = LatestValueStore()
    store.update(_tag("d1/t1", 1))
    store.update(_tag("d1/t1", 2))

    [tag] = store.for_driver("d1")
    assert tag.value == 2


def test_for_driver_returns_empty_list_for_unknown_instance() -> None:
    store = LatestValueStore()
    store.update(_tag("d1/t1", 1))

    assert store.for_driver("unknown") == []


def test_for_driver_does_not_match_a_prefix_that_isnt_a_full_instance_id() -> None:
    # "d1" must not match a differently-named instance that merely starts
    # with the same characters (e.g. "d10") — the "/" separator makes the
    # match exact, not a bare string prefix.
    store = LatestValueStore()
    store.update(_tag("d10/t1", 1))

    assert store.for_driver("d1") == []
