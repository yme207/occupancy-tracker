"""Learned engine-statistics store.

Persists `OccupancyEngine`'s self-tuned statistics across Home Assistant
restarts — genuinely accumulated, house-specific data built up over weeks,
unlike `topology_store.py`'s user-authored config: learned per-Area-pair
transit-timing (docs/DECISIONS.md's "learned transit timing" entry), and
learned per-Area sensor-reliability miss counts (docs/DECISIONS.md's
"per-Area sensor reliability" entry). Deliberately a separate `Store`/module
from `topology_store.py`: different owner (the engine infers these on its
own, the user never edits them directly), different lifecycle (changes on
almost every clean transit, not just on a deliberate topology edit), and no
reason to couple their schemas together. The two kinds of statistics share
one `Store`/file rather than each getting its own, since they're both small,
both engine-owned, and always saved together off the same "belief state
changed" listener (`__init__.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypedDict

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .registry_sync import HouseShape

STORAGE_VERSION_MAJOR = 1
#: Bumped 0 -> 1 to add `sensor_reliability` alongside `pairs`. Purely
#: additive — verified from `Store._async_load_data`'s source: when the major
#: version still matches but the minor doesn't, the base `Store._async_migrate_func`
#: raises `NotImplementedError`, which the loader catches and falls back to
#: returning the stored data unchanged (rather than raising) whenever the
#: major version matches. So data written by the previous minor version loads
#: back exactly as before; `sensor_reliability_from_dict` below just needs to
#: tolerate that key being absent from it.
STORAGE_VERSION_MINOR = 1
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
    #: Area id -> accumulated "found empty during a resolved transit search"
    #: count (docs/DECISIONS.md's "per-Area sensor reliability" entry). A
    #: plain `dict[str, int]` round-trips through JSON directly, unlike
    #: `pairs` above (no `frozenset` key to work around).
    sensor_reliability: dict[str, int]


def learned_timing_to_dict(
    stats: LearnedTransitTimes, sensor_reliability: Mapping[str, int]
) -> LearnedTimingDict:
    """Convert an `OccupancyEngine.learned_transit_times()` snapshot (plus its
    `learned_sensor_reliability()` counterpart) to a plain, JSON-serializable
    dict — a `frozenset` isn't JSON-safe, so each transit-time pair is stored
    as two plain fields instead.
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
    return {"pairs": pairs, "sensor_reliability": dict(sensor_reliability)}


def learned_timing_from_dict(
    data: LearnedTimingDict,
) -> dict[frozenset[str], tuple[int, float, float]]:
    """The transit-timing half of `learned_timing_to_dict`'s inverse — also
    the shape fed straight into `OccupancyEngine`'s `learned_transit_times`
    constructor argument.
    """
    return {
        frozenset({pair["area_a"], pair["area_b"]}): (
            pair["count"],
            pair["mean_seconds"],
            pair["m2_seconds"],
        )
        for pair in data["pairs"]
    }


def sensor_reliability_from_dict(data: LearnedTimingDict) -> dict[str, int]:
    """The sensor-reliability half of `learned_timing_to_dict`'s inverse —
    also the shape fed straight into `OccupancyEngine`'s
    `learned_sensor_reliability` constructor argument. Uses `.get()` rather
    than `data["sensor_reliability"]` since data written before this key
    existed (`STORAGE_VERSION_MINOR` 0) loads back with it simply absent, not
    migrated in (see `STORAGE_VERSION_MINOR`'s own docstring).
    """
    return dict(data.get("sensor_reliability", {}))


class LearnedTimingStore:
    """Loads, debounced-saves, and reconciles the engine's learned statistics
    (per-Area-pair transit timing, per-Area sensor reliability).
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[LearnedTimingDict] = Store(
            hass,
            STORAGE_VERSION_MAJOR,
            f"{_STORAGE_KEY_PREFIX}_{entry_id}",
            minor_version=STORAGE_VERSION_MINOR,
        )
        self._data: dict[frozenset[str], tuple[int, float, float]] = {}
        self._sensor_reliability: dict[str, int] = {}

    @property
    def data(self) -> Mapping[frozenset[str], tuple[int, float, float]]:
        """The currently-loaded learned timing data — feed straight into
        `OccupancyEngine`'s `learned_transit_times` constructor argument.
        """
        return self._data

    @property
    def sensor_reliability(self) -> Mapping[str, int]:
        """The currently-loaded learned sensor-reliability data — feed
        straight into `OccupancyEngine`'s `learned_sensor_reliability`
        constructor argument.
        """
        return self._sensor_reliability

    async def async_load(self) -> None:
        """Load the persisted learned statistics, if any."""
        stored = await self._store.async_load()
        if stored is not None:
            self._data = learned_timing_from_dict(stored)
            self._sensor_reliability = sensor_reliability_from_dict(stored)

    @callback
    def async_schedule_save(
        self,
        transit_times_func: Callable[[], LearnedTransitTimes],
        sensor_reliability_func: Callable[[], Mapping[str, int]],
    ) -> None:
        """Schedule a debounced save. Both `_func` arguments (typically the
        live engine's own `learned_transit_times`/`learned_sensor_reliability`
        methods) are called at actual write time, not now — verified from
        `Store.async_delay_save`'s own contract — so a burst of
        `add_listener` callbacks scheduling this repeatedly in quick
        succession still only ever writes the latest state once, not a stale
        snapshot from whenever the first one fired.
        """
        self._store.async_delay_save(
            lambda: learned_timing_to_dict(transit_times_func(), sensor_reliability_func()),
            delay=_SAVE_DELAY_SECONDS,
        )

    def reconcile(
        self, house_shape: HouseShape
    ) -> Mapping[frozenset[str], tuple[int, float, float]]:
        """Strip any learned data referencing an Area that no longer exists.

        Unlike `TopologyStore.reconcile()`, this isn't guarding against a
        real correctness problem (a stale entry for a since-deleted Area is
        simply never looked up again — `OccupancyEngine` only ever queries
        by Area ids in its *current* `HouseGraph`), just hygiene: no reason
        to keep persisting learned data about rooms that no longer exist.
        """
        self._data = {
            key: value
            for key, value in self._data.items()
            if all(area_id in house_shape.areas for area_id in key)
        }
        self._sensor_reliability = {
            area_id: count
            for area_id, count in self._sensor_reliability.items()
            if area_id in house_shape.areas
        }
        return self._data
