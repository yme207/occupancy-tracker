"""Tests for the learned engine-statistics store (docs/DECISIONS.md's
"learned transit timing" and "per-Area sensor reliability" entries).
"""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.core import HomeAssistant

from custom_components.occupancy_tracker.learned_timing_store import (
    LearnedTimingStore,
    learned_timing_from_dict,
    learned_timing_to_dict,
    sensor_reliability_from_dict,
)
from custom_components.occupancy_tracker.registry_sync import AreaSnapshot, HouseShape


def _house_shape(area_ids: tuple[str, ...]) -> HouseShape:
    areas = {
        area_id: AreaSnapshot(area_id=area_id, name=area_id, floor_id=None, entity_ids=())
        for area_id in area_ids
    }
    return HouseShape(
        areas=MappingProxyType(areas), floors=MappingProxyType({}), entities=MappingProxyType({})
    )


def test_learned_timing_to_dict_and_back_round_trips() -> None:
    stats = {
        frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0),
        frozenset({"landing", "office"}): (5, 8.0, 0.0),
    }

    round_tripped = learned_timing_from_dict(learned_timing_to_dict(stats, {}))

    assert round_tripped == stats


def test_sensor_reliability_to_dict_and_back_round_trips() -> None:
    reliability = {"landing": 5, "stairs": 2}

    round_tripped = sensor_reliability_from_dict(learned_timing_to_dict({}, reliability))

    assert round_tripped == reliability


def test_sensor_reliability_from_dict_tolerates_data_written_before_the_key_existed() -> None:
    """`STORAGE_VERSION_MINOR` 0 -> 1 was purely additive — data saved by the
    previous minor version loads back with `sensor_reliability` simply
    absent, not migrated in (see that constant's own docstring).
    """
    assert sensor_reliability_from_dict({"pairs": []}) == {}  # type: ignore[typeddict-item]


def test_learned_timing_to_dict_orders_area_ids_deterministically() -> None:
    """Sorted, not insertion-order — a `frozenset` has no defined iteration
    order, so serialization has to pick a canonical one to be stable.
    """
    stats = {frozenset({"zebra", "alpha"}): (5, 1.0, 0.0)}

    result = learned_timing_to_dict(stats, {})

    assert result["pairs"] == [
        {"area_a": "alpha", "area_b": "zebra", "count": 5, "mean_seconds": 1.0, "m2_seconds": 0.0}
    ]


async def test_default_learned_timing_is_empty(hass: HomeAssistant) -> None:
    store = LearnedTimingStore(hass, "entry-1")
    await store.async_load()

    assert store.data == {}
    assert store.sensor_reliability == {}


async def test_save_and_load_roundtrip(hass: HomeAssistant) -> None:
    stats = {frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0)}
    reliability = {"landing": 3}

    writer = LearnedTimingStore(hass, "entry-1")
    # Bypasses the debounced async_schedule_save path deliberately — this
    # test is about the persisted *format* round-tripping correctly, not
    # about the delayed-write scheduling itself.
    await writer._store.async_save(learned_timing_to_dict(stats, reliability))

    reader = LearnedTimingStore(hass, "entry-1")
    await reader.async_load()

    assert reader.data == stats
    assert reader.sensor_reliability == reliability


async def test_different_entries_do_not_share_storage(hass: HomeAssistant) -> None:
    stats = {frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0)}
    store_a = LearnedTimingStore(hass, "entry-a")
    await store_a._store.async_save(learned_timing_to_dict(stats, {}))

    store_b = LearnedTimingStore(hass, "entry-b")
    await store_b.async_load()

    assert store_b.data == {}
    assert store_b.sensor_reliability == {}


def test_reconcile_keeps_pair_when_both_areas_exist(hass: HomeAssistant) -> None:
    store = LearnedTimingStore(hass, "entry-1")
    store._data = {frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0)}

    result = store.reconcile(_house_shape(("kitchen", "hallway")))

    assert result == {frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0)}


def test_reconcile_drops_pair_referencing_a_missing_area(hass: HomeAssistant) -> None:
    store = LearnedTimingStore(hass, "entry-1")
    store._data = {
        frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0),
        frozenset({"landing", "office"}): (5, 8.0, 0.0),
    }

    result = store.reconcile(_house_shape(("kitchen", "hallway")))  # landing/office gone

    assert result == {frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0)}
    assert store.data == result


def test_reconcile_drops_sensor_reliability_referencing_a_missing_area(
    hass: HomeAssistant,
) -> None:
    store = LearnedTimingStore(hass, "entry-1")
    store._sensor_reliability = {"kitchen": 4, "landing": 2}

    store.reconcile(_house_shape(("kitchen",)))  # landing gone

    assert store.sensor_reliability == {"kitchen": 4}


async def test_async_schedule_save_defers_calling_data_funcs_until_write_time(
    hass: HomeAssistant,
) -> None:
    """Both `_func` arguments must be called at actual write time, not when
    scheduled — otherwise a burst of calls in quick succession would persist
    a stale snapshot from whichever one fired first, not the latest state.
    """
    store = LearnedTimingStore(hass, "entry-1")
    calls = 0

    def transit_times_func() -> dict[frozenset[str], tuple[int, float, float]]:
        nonlocal calls
        calls += 1
        return {frozenset({"kitchen", "hallway"}): (calls, 1.0, 0.0)}

    def sensor_reliability_func() -> dict[str, int]:
        return {"landing": calls}

    store.async_schedule_save(transit_times_func, sensor_reliability_func)

    # Scheduling alone must not have called either func yet.
    assert calls == 0
