"""Learned transit-timing store.

Persists `OccupancyEngine`'s learned per-Area-pair transit-timing statistics
(docs/DECISIONS.md's "learned transit timing" entry) across Home Assistant
restarts — genuinely accumulated, house-specific data built up over weeks,
unlike `topology_store.py`'s user-authored config. Deliberately a separate
`Store`/module from `topology_store.py`: different owner (the engine infers
this on its own, the user never edits it directly), different lifecycle
(changes on almost every clean transit, not just on a deliberate topology
edit), and no reason to couple their schemas together.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypedDict

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .registry_sync import HouseShape

STORAGE_VERSION_MAJOR = 1
STORAGE_VERSION_MINOR = 0
_STORAGE_KEY_PREFIX = "occupancy_tracker_learned_timing"
#: How long to wait after a learning update before actually writing to disk
#: (`homeassistant.helpers.storage.Store.async_delay_save`, verified from
#: source: it coalesces repeated calls within this window into a single
#: write) — a burst of activity walking through several rooms shouldn't mean
#: a disk write per hop.
_SAVE_DELAY_SECONDS = 30

#: What `OccupancyEngine.learned_transit_times()`/its constructor's
#: `learned_transit_times` argument both use: unordered Area-pair -> (count,
#: mean_seconds, m2_seconds). Defined here (not imported from
#: `occupancy_engine.py`) to avoid this HA-dependent module reaching into
#: the engine's internals for a type alias — the shape is small and stable
#: enough to just restate.
LearnedTransitTimes = Mapping[frozenset[str], tuple[int, float, float]]


class _TransitTimePairDict(TypedDict):
    area_a: str
    area_b: str
    count: int
    mean_seconds: float
    m2_seconds: float


class LearnedTimingDict(TypedDict):
    pairs: list[_TransitTimePairDict]


def learned_timing_to_dict(stats: LearnedTransitTimes) -> LearnedTimingDict:
    """Convert an `OccupancyEngine.learned_transit_times()` snapshot to a
    plain, JSON-serializable dict — a `frozenset` isn't JSON-safe, so each
    pair is stored as two plain fields instead.
    """
    pairs: list[_TransitTimePairDict] = []
    for key, (count, mean_seconds, m2_seconds) in stats.items():
        area_a, area_b = sorted(key)
        pairs.append(
            {
                "area_a": area_a,
                "area_b": area_b,
                "count": count,
                "mean_seconds": mean_seconds,
                "m2_seconds": m2_seconds,
            }
        )
    return {"pairs": pairs}


def learned_timing_from_dict(
    data: LearnedTimingDict,
) -> dict[frozenset[str], tuple[int, float, float]]:
    """Inverse of `learned_timing_to_dict` — also the shape fed straight into
    `OccupancyEngine`'s `learned_transit_times` constructor argument.
    """
    return {
        frozenset({pair["area_a"], pair["area_b"]}): (
            pair["count"],
            pair["mean_seconds"],
            pair["m2_seconds"],
        )
        for pair in data["pairs"]
    }


class LearnedTimingStore:
    """Loads, debounced-saves, and reconciles learned per-Area-pair transit timing."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[LearnedTimingDict] = Store(
            hass,
            STORAGE_VERSION_MAJOR,
            f"{_STORAGE_KEY_PREFIX}_{entry_id}",
            minor_version=STORAGE_VERSION_MINOR,
        )
        self._data: dict[frozenset[str], tuple[int, float, float]] = {}

    @property
    def data(self) -> Mapping[frozenset[str], tuple[int, float, float]]:
        """The currently-loaded learned timing data — feed straight into
        `OccupancyEngine`'s `learned_transit_times` constructor argument.
        """
        return self._data

    async def async_load(self) -> None:
        """Load the persisted learned timing data, if any."""
        stored = await self._store.async_load()
        if stored is not None:
            self._data = learned_timing_from_dict(stored)

    @callback
    def async_schedule_save(self, data_func: Callable[[], LearnedTransitTimes]) -> None:
        """Schedule a debounced save. `data_func` (typically the live
        engine's own `learned_transit_times` method) is called at actual
        write time, not now — verified from `Store.async_delay_save`'s own
        contract — so a burst of `add_listener` callbacks scheduling this
        repeatedly in quick succession still only ever writes the latest
        state once, not a stale snapshot from whenever the first one fired.
        """
        self._store.async_delay_save(
            lambda: learned_timing_to_dict(data_func()), delay=_SAVE_DELAY_SECONDS
        )

    def reconcile(
        self, house_shape: HouseShape
    ) -> Mapping[frozenset[str], tuple[int, float, float]]:
        """Strip any learned pair referencing an Area that no longer exists.

        Unlike `TopologyStore.reconcile()`, this isn't guarding against a
        real correctness problem (a stale pair for a since-deleted Area is
        simply never looked up again — `OccupancyEngine` only ever queries
        by Area ids in its *current* `HouseGraph`), just hygiene: no reason
        to keep persisting learned data about rooms that no longer exist.
        """
        self._data = {
            key: value
            for key, value in self._data.items()
            if all(area_id in house_shape.areas for area_id in key)
        }
        return self._data
