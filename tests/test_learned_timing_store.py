"""Tests for the learned transit-timing store (docs/DECISIONS.md's "learned
transit timing" entry).
"""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.core import HomeAssistant

from custom_components.occupancy_tracker.learned_timing_store import (
    LearnedTimingStore,
    learned_timing_from_dict,
    learned_timing_to_dict,
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

    round_tripped = learned_timing_from_dict(learned_timing_to_dict(stats))

    assert round_tripped == stats


def test_learned_timing_to_dict_orders_area_ids_deterministically() -> None:
    """Sorted, not insertion-order — a `frozenset` has no defined iteration
    order, so serialization has to pick a canonical one to be stable.
    """
    stats = {frozenset({"zebra", "alpha"}): (5, 1.0, 0.0)}

    result = learned_timing_to_dict(stats)

    assert result["pairs"] == [
        {"area_a": "alpha", "area_b": "zebra", "count": 5, "mean_seconds": 1.0, "m2_seconds": 0.0}
    ]


async def test_default_learned_timing_is_empty(hass: HomeAssistant) -> None:
    store = LearnedTimingStore(hass, "entry-1")
    await store.async_load()

    assert store.data == {}


async def test_save_and_load_roundtrip(hass: HomeAssistant) -> None:
    stats = {frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0)}

    writer = LearnedTimingStore(hass, "entry-1")
    # Bypasses the debounced async_schedule_save path deliberately — this
    # test is about the persisted *format* round-tripping correctly, not
    # about the delayed-write scheduling itself.
    await writer._store.async_save(learned_timing_to_dict(stats))

    reader = LearnedTimingStore(hass, "entry-1")
    await reader.async_load()

    assert reader.data == stats


async def test_different_entries_do_not_share_storage(hass: HomeAssistant) -> None:
    stats = {frozenset({"kitchen", "hallway"}): (6, 12.5, 40.0)}
    store_a = LearnedTimingStore(hass, "entry-a")
    await store_a._store.async_save(learned_timing_to_dict(stats))

    store_b = LearnedTimingStore(hass, "entry-b")
    await store_b.async_load()

    assert store_b.data == {}


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


async def test_async_schedule_save_defers_calling_data_func_until_write_time(
    hass: HomeAssistant,
) -> None:
    """`data_func` must be called at actual write time, not when scheduled —
    otherwise a burst of calls in quick succession would persist a stale
    snapshot from whichever one fired first, not the latest state.
    """
    store = LearnedTimingStore(hass, "entry-1")
    calls = 0

    def data_func() -> dict[frozenset[str], tuple[int, float, float]]:
        nonlocal calls
        calls += 1
        return {frozenset({"kitchen", "hallway"}): (calls, 1.0, 0.0)}

    store.async_schedule_save(data_func)

    # Scheduling alone must not have called data_func yet.
    assert calls == 0
