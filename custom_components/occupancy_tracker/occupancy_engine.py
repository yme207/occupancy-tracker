"""Occupancy engine: the latch/transit-inference state machine.

Pure Python, no Home Assistant dependency (docs/ARCHITECTURE.md §1.4) —
consumes a `HouseGraph` (a standalone graph, not `topology_store.TopologyData`
or `registry_sync.HouseShape` directly, so this module never imports
Home Assistant even transitively) plus a stream of `Signal`s, and produces
per-Area occupant state (docs/SPEC.md §6.2-§6.5). Building a `HouseGraph`
from the live topology store + registry sync layer is a later phase's job
(docs/ARCHITECTURE.md §1.4-1.5: entity platforms wire the shared engine
instance to those layers).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto

#: Sentinel "area id" for the boundary outside the house (docs/SPEC.md §6.1).
#: Never a real Area, never present in a HouseGraph's `area_ids`, but valid as
#: one endpoint of an egress Connector.
OUTSIDE = "outside"


@dataclass(frozen=True, slots=True)
class GraphConnector:
    """An edge in the house graph: a passage between two nodes.

    One side may be `OUTSIDE`, representing an egress point (SPEC.md §6.5) —
    the engine treats egress-point crossings like any other Connector, with
    "outside" as an ordinary (if occupant-count-less) graph node.
    """

    connector_id: str
    area_id_a: str
    area_id_b: str

    def other_side(self, area_id: str) -> str:
        if area_id == self.area_id_a:
            return self.area_id_b
        if area_id == self.area_id_b:
            return self.area_id_a
        raise ValueError(f"{area_id!r} is not an endpoint of connector {self.connector_id!r}")

    def touches(self, area_id: str) -> bool:
        return area_id in (self.area_id_a, self.area_id_b)


@dataclass(frozen=True, slots=True)
class HouseGraph:
    """All Areas + Connectors the engine reasons over (SPEC.md §6.1)."""

    area_ids: frozenset[str]
    connectors: tuple[GraphConnector, ...] = ()

    def connector(self, connector_id: str) -> GraphConnector:
        for connector in self.connectors:
            if connector.connector_id == connector_id:
                return connector
        raise ValueError(f"Unknown connector: {connector_id!r}")


class ProvenanceTier(Enum):
    """Automation-vs-manual causal classification of a Signal (SPEC.md §6.6).

    Defined here (not in `provenance.py`) so `occupancy_engine.py` stays
    HA-import-free — resolving a real `homeassistant.core.Context` into one
    of these tiers is `provenance.py`'s job; this module only needs the tag
    itself (see docs/DECISIONS.md).
    """

    #: `Context` chain resolves to a known automation/script — suppressed
    #: entirely by signal ingestion before it ever reaches the engine (high
    #: confidence it's machine-caused, so it never becomes a Signal at all).
    AUTOMATION_SUPPRESSED = auto()
    #: `Context.user_id` present, no automation ancestry — high confidence
    #: it's human-caused.
    USER_CONFIRMED = auto()
    #: Neither an automation ancestor nor a `user_id` — a device acting on
    #: its own most consistent with someone physically operating it, but
    #: weaker evidence than a direct user match.
    AMBIGUOUS_PHYSICAL = auto()


@dataclass(frozen=True, slots=True)
class AreaActivitySignal:
    """Evidence someone is active *in* `area_id` right now."""

    area_id: str
    timestamp: datetime
    source: str
    provenance: ProvenanceTier = ProvenanceTier.USER_CONFIRMED


@dataclass(frozen=True, slots=True)
class ConnectorActivitySignal:
    """Evidence of crossing activity along `connector_id` (SPEC.md §6.3)."""

    connector_id: str
    timestamp: datetime
    source: str
    provenance: ProvenanceTier = ProvenanceTier.USER_CONFIRMED


#: The only shape the engine consumes (docs/ARCHITECTURE.md §1.3).
#: `AUTOMATION_SUPPRESSED` signals are never constructed by signal ingestion
#: in the first place (docs/DECISIONS.md) — the engine only ever sees
#: `USER_CONFIRMED`/`AMBIGUOUS_PHYSICAL` evidence, both accepted, tracked as
#: an inspectable attribute (SPEC.md §6.8) rather than weighted numerically.
Signal = AreaActivitySignal | ConnectorActivitySignal


class StateQuality(Enum):
    """SPEC.md §6.8's confirmed/latched/ambiguous tiers."""

    #: Directly evidenced within the freshness window.
    CONFIRMED = auto()
    #: No recent direct evidence, but retained via the latch — the count
    #: itself never decays (SPEC.md §6.2), only this label does.
    LATCHED = auto()
    #: An unresolved pending transit currently touches this Area.
    AMBIGUOUS = auto()


@dataclass(frozen=True, slots=True)
class AreaState:
    """Point-in-time occupancy belief for one Area."""

    area_id: str
    occupant_count: int
    quality: StateQuality
    last_confirmed: datetime | None
    #: Provenance of the most recent direct evidence for this Area — an
    #: inspectable attribute (SPEC.md §6.8), not a numeric weight the engine
    #: itself branches on (see docs/DECISIONS.md).
    last_provenance: ProvenanceTier | None


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Tunables for the latch/transit model (docs/ARCHITECTURE.md §2's
    "typed, centralized config" extension point). Scoped to what SPEC.md
    §6.2-§6.5 needs; household-size hints, confidence thresholds, and
    near-house zones join this (or a config object that wraps it) in later
    phases as their features land, rather than being stubbed in now.
    """

    #: How long a Connector-activity event waits for destination-Area
    #: corroboration before being discarded unconfirmed (SPEC.md §6.3). Also
    #: doubles as the upper bound on how long after a candidate source Area's
    #: last evidence a sensor-less-Connector direct transit stays plausible.
    transit_confirmation_window: timedelta = timedelta(seconds=90)
    #: How long after direct evidence an Area's quality stays CONFIRMED
    #: before degrading to LATCHED. The occupant *count* never changes from
    #: this — only the freshness label (SPEC.md §6.2, §6.8).
    confirmed_freshness_window: timedelta = timedelta(minutes=10)
    #: Lower bound on how soon after a candidate source Area's last evidence
    #: a sensor-less-Connector direct transit can plausibly be the *same*
    #: person: a gap shorter than this is physically impossible (they can't
    #: teleport), so it's read as a second, independent occupant instead.
    min_transit_time: timedelta = timedelta(seconds=2)
    #: Optional whole-house "typical household size" confidence hint (SPEC.md
    #: §6.4) — deliberately *not* consulted anywhere in the count-inference
    #: logic above (`_handle_area_activity` et al.): SPEC.md is explicit this
    #: "must never reject or cap a count the evidence actually supports," so
    #: it's surfaced only as a separate, purely observational attribute
    #: (`exceeds_household_size_hint`, `sensor.py`'s `TotalOccupantCountSensor`)
    #: rather than woven into the state machine, where it would risk becoming
    #: a de facto cap through some later refactor.
    household_size_hint: int | None = None


@dataclass(slots=True)
class _PendingTransit:
    connector_id: str
    source_area_id: str  # may be OUTSIDE
    dest_area_id: str  # may be OUTSIDE
    expires_at: datetime


class OccupancyEngine:
    """The latch/transit-inference state machine (SPEC.md §6.2-§6.5)."""

    def __init__(self, graph: HouseGraph, config: EngineConfig | None = None) -> None:
        self._graph = graph
        self._config = config or EngineConfig()
        self._counts: dict[str, int] = dict.fromkeys(graph.area_ids, 0)
        self._last_confirmed: dict[str, datetime | None] = dict.fromkeys(graph.area_ids)
        self._last_provenance: dict[str, ProvenanceTier | None] = dict.fromkeys(graph.area_ids)
        self._pending: dict[str, _PendingTransit] = {}
        self._listeners: list[Callable[[], None]] = []

    @property
    def graph(self) -> HouseGraph:
        """The `HouseGraph` this engine was built from — read-only, for callers that need to
        resolve a Connector id (e.g. from `pending_transit_connector_ids`) back to the Areas it
        connects (SPEC.md §7.3's explainability inspector) without duplicating graph-construction
        logic outside `engine_adapter.py`. `HouseGraph` is itself a frozen dataclass, so this
        can't be used to mutate engine state through the back door.
        """
        return self._graph

    @property
    def household_size_hint(self) -> int | None:
        """The configured "typical household size" confidence hint (SPEC.md §6.4), if any."""
        return self._config.household_size_hint

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback invoked after any Signal changes belief state.

        Entity platforms (Phase 4) use this to push state updates instead of
        polling (docs/ARCHITECTURE.md §1.5, §9). Time-driven-only transitions
        (a quality tier degrading purely because time passed, with no new
        Signal) do *not* trigger this — see docs/STATUS.md's Phase 4 entry
        for why that's an accepted, documented gap for now rather than a
        scheduled-timer mechanism built speculatively ahead of need.
        """
        self._listeners.append(listener)

        def remove_listener() -> None:
            self._listeners.remove(listener)

        return remove_listener

    def area_state(self, area_id: str, now: datetime) -> AreaState:
        """Return the current belief about `area_id` as of `now`."""
        if area_id not in self._counts:
            raise ValueError(f"Unknown area: {area_id!r}")
        self._expire_pending(now)
        return AreaState(
            area_id=area_id,
            occupant_count=self._counts[area_id],
            quality=self._quality_for(area_id, now),
            last_confirmed=self._last_confirmed[area_id],
            last_provenance=self._last_provenance[area_id],
        )

    def all_area_states(self, now: datetime) -> dict[str, AreaState]:
        """Return every Area's current state as of `now`."""
        return {area_id: self.area_state(area_id, now) for area_id in self._graph.area_ids}

    def total_occupant_count(self, now: datetime) -> int:
        """Whole-house occupant total (SPEC.md §8's house-level entity)."""
        self._expire_pending(now)
        return sum(self._counts.values())

    def pending_transit_connector_ids(self, now: datetime) -> frozenset[str]:
        """Connector ids with an unresolved transit, for diagnostics (SPEC.md §8)."""
        self._expire_pending(now)
        return frozenset(self._pending)

    def override_occupant_count(self, area_id: str, count: int, now: datetime) -> None:
        """Directly set an Area's occupant count (SPEC.md §8's manual override service).

        Bypasses the latch/transit machinery entirely — this is for the rare case a person
        corrects a wrong automatic inference on the spot, not a new kind of `Signal` the engine
        reasons about. Clears any pending transit touching this Area, since a manual correction is
        strictly more authoritative than an unresolved automatic guess about the same Area, and
        leaving it in place could otherwise later resolve the count elsewhere out from under it.
        """
        if area_id not in self._counts:
            raise ValueError(f"Unknown area: {area_id!r}")
        if count < 0:
            raise ValueError(f"Occupant count cannot be negative: {count!r}")
        self._expire_pending(now)
        for connector_id in [
            connector_id
            for connector_id, pending in self._pending.items()
            if pending.source_area_id == area_id or pending.dest_area_id == area_id
        ]:
            del self._pending[connector_id]
        self._counts[area_id] = count
        self._last_confirmed[area_id] = now
        self._last_provenance[area_id] = ProvenanceTier.USER_CONFIRMED
        for listener in list(self._listeners):
            listener()

    def process_signal(self, signal: Signal) -> None:
        """Fold one normalized Signal into the engine's belief state."""
        self._expire_pending(signal.timestamp)
        if isinstance(signal, AreaActivitySignal):
            self._handle_area_activity(signal)
        else:
            self._handle_connector_activity(signal)
        for listener in list(self._listeners):
            listener()

    # -- internals --------------------------------------------------------

    def _quality_for(self, area_id: str, now: datetime) -> StateQuality:
        if any(
            p.source_area_id == area_id or p.dest_area_id == area_id for p in self._pending.values()
        ):
            return StateQuality.AMBIGUOUS
        last_confirmed = self._last_confirmed[area_id]
        if (
            last_confirmed is not None
            and now - last_confirmed <= self._config.confirmed_freshness_window
        ):
            return StateQuality.CONFIRMED
        return StateQuality.LATCHED

    def _expire_pending(self, now: datetime) -> None:
        # Timing out a pending transit makes no count change at all: "no
        # corroborating Connector activity → occupant is assumed to still be
        # in the [source] Area" (SPEC.md §6.3).
        expired = [
            connector_id
            for connector_id, pending in self._pending.items()
            if now >= pending.expires_at
        ]
        for connector_id in expired:
            del self._pending[connector_id]

    def _handle_area_activity(self, signal: AreaActivitySignal) -> None:
        area_id = signal.area_id
        if area_id not in self._counts:
            raise ValueError(f"Unknown area: {area_id!r}")

        pending = self._pending_for_dest(area_id)
        if pending is not None:
            self._confirm_transit(pending, signal.timestamp, signal.provenance)
            return

        if self._counts[area_id] == 0:
            # No Connector-sensor-driven pending transit explains this.
            # Most Connectors have no sensor of their own to produce one
            # (SPEC.md §7.3 only lets users bind entities to egress points,
            # not ordinary Connector edges; §5.1 explicitly allows a
            # sensor-less connecting Area) — so for those, destination
            # activity plus topology adjacency is the *only* observable
            # evidence of a transit at all, and it has to double as both
            # "candidate evidence" and "corroboration" in the same event
            # (see docs/DECISIONS.md).
            source = self._plausible_transit_source(area_id, signal.timestamp)
            if source is not None:
                self._counts[source] -= 1
                self._counts[area_id] = 1
                self._last_confirmed[source] = signal.timestamp
                self._last_confirmed[area_id] = signal.timestamp
                self._last_provenance[source] = signal.provenance
                self._last_provenance[area_id] = signal.provenance
                return
            # No Connector-adjacent Area plausibly explains this as the same
            # person arriving — new-occupant evidence (SPEC.md §6.4).
            self._counts[area_id] = 1

        self._last_confirmed[area_id] = signal.timestamp
        self._last_provenance[area_id] = signal.provenance

    def _plausible_transit_source(self, area_id: str, now: datetime) -> str | None:
        """The one reachable occupied Area whose last evidence is close
        enough in time to `now` to plausibly be the same person arriving on
        foot — not so close it would require teleporting (they can't — a gap
        under `min_transit_time` means it's a second, distinct occupant
        instead), not so long ago it's more likely unrelated (see
        docs/DECISIONS.md).

        "Reachable" walks through any chain of currently-*empty* adjacent
        Areas transparently (SPEC.md §5.1: "an Area with zero devices/
        entities is still a valid graph node... e.g. a hallway with no
        sensors at all, connecting two sensored rooms" — an empty Area here
        isn't necessarily literally sensor-less, just currently unconfirmed,
        but the effect needed is the same either way: it must not block the
        search for a real, occupied source just because it's the empty
        Area in between).

        Breadth-first, nearest candidates only: a real 1-hop neighbor is
        always at least as plausible as some other occupied Area three empty
        rooms away that merely *also* happens to fall inside the same flat
        `transit_confirmation_window` — walking further genuinely takes more
        time, so a closer candidate should never lose out to a coincidentally
        time-plausible farther one. Once any candidate is found at a given
        distance, farther distances are never even considered; ambiguity
        (returning `None`) only applies among ties at that same, nearest
        distance — found via a real scripted scenario where a farther Area
        reachable through an unrelated unoccupied chain otherwise polluted a
        much more obviously correct 1-hop match (see docs/DECISIONS.md).
        """
        visited = {area_id}
        frontier = {area_id}
        while frontier:
            candidates: set[str] = set()
            next_frontier: set[str] = set()
            for current in frontier:
                for connector in self._graph.connectors:
                    if not connector.touches(current):
                        continue
                    other = connector.other_side(current)
                    if other == OUTSIDE or other in visited:
                        continue
                    visited.add(other)
                    if self._counts.get(other, 0) <= 0:
                        # Empty — not a candidate itself, but keep looking
                        # further out along this chain if nothing closer pans out.
                        next_frontier.add(other)
                        continue
                    last_confirmed = self._last_confirmed.get(other)
                    if last_confirmed is None:
                        continue
                    gap = now - last_confirmed
                    if (
                        self._config.min_transit_time
                        <= gap
                        <= self._config.transit_confirmation_window
                    ):
                        candidates.add(other)
            if candidates:
                return next(iter(candidates)) if len(candidates) == 1 else None
            frontier = next_frontier
        return None

    def _handle_connector_activity(self, signal: ConnectorActivitySignal) -> None:
        connector = self._graph.connector(signal.connector_id)

        if connector.touches(OUTSIDE):
            self._handle_egress_activity(connector, signal)
            return

        area_a, area_b = connector.area_id_a, connector.area_id_b
        count_a, count_b = self._counts[area_a], self._counts[area_b]
        if count_a > 0 and count_b == 0:
            source, dest = area_a, area_b
        elif count_b > 0 and count_a == 0:
            source, dest = area_b, area_a
        else:
            # Both occupied or both empty: this Connector event alone can't
            # resolve a direction, so it's not actionable evidence on its
            # own (SPEC.md doesn't specify a resolution for this case; a
            # conservative "no inference" is the safe default — see
            # docs/DECISIONS.md).
            return

        self._register_pending(connector.connector_id, source, dest, signal.timestamp)

    def _handle_egress_activity(
        self, connector: GraphConnector, signal: ConnectorActivitySignal
    ) -> None:
        inside = connector.other_side(OUTSIDE)
        if self._counts[inside] > 0:
            # Departure: the egress Area was occupied, so the crossing
            # sensor itself is the confirming evidence — OUTSIDE can never
            # corroborate, so unlike a regular transit this confirms
            # immediately rather than waiting on a pending window (see
            # docs/DECISIONS.md).
            self._counts[inside] -= 1
            self._last_confirmed[inside] = signal.timestamp
            self._last_provenance[inside] = signal.provenance
            self._pending.pop(connector.connector_id, None)
        else:
            # Possible arrival: a door/window crossing alone isn't proof
            # someone came in (wind, a pet, retrieving a parcel) — wait for
            # the egress Area's own activity to corroborate.
            self._register_pending(connector.connector_id, OUTSIDE, inside, signal.timestamp)

    def _register_pending(
        self, connector_id: str, source: str, dest: str, fired_at: datetime
    ) -> None:
        self._pending[connector_id] = _PendingTransit(
            connector_id=connector_id,
            source_area_id=source,
            dest_area_id=dest,
            expires_at=fired_at + self._config.transit_confirmation_window,
        )

    def _pending_for_dest(self, area_id: str) -> _PendingTransit | None:
        for pending in self._pending.values():
            if pending.dest_area_id == area_id:
                return pending
        return None

    def _confirm_transit(
        self, pending: _PendingTransit, now: datetime, provenance: ProvenanceTier
    ) -> None:
        source = pending.source_area_id
        if source == OUTSIDE:
            # An egress-point arrival's source is always modeled as OUTSIDE
            # by the connector itself, since only the door sensor fired —
            # but a Connector-adjacent interior Area that was *also* freshly
            # occupied (e.g. a front-yard motion sensor tripping on the walk
            # up to the door, a few seconds before the door itself) is a
            # more plausible true source than a brand-new arrival: without
            # this check, that Area's occupant is left stranded there
            # forever, phantom-duplicating the same person as both "still in
            # the yard" and "a new arrival" (found via a real scripted
            # walkthrough scenario against the actual house topology, not
            # theorized — see docs/DECISIONS.md). Reuses the same
            # timing+adjacency heuristic the sensor-less-connector path
            # already relies on everywhere else, rather than inventing a
            # second one — if more than one neighbor is plausible, it stays
            # ambiguous and falls back to OUTSIDE, same as that path's own
            # "can't resolve a direction" behavior.
            alt_source = self._plausible_transit_source(pending.dest_area_id, now)
            if alt_source is not None:
                source = alt_source

        if source != OUTSIDE and self._counts[source] <= 0:
            # The source was already drained by a different confirmed
            # transit while this one was pending (two Connectors both
            # inferring the same Area as source) — the original inference no
            # longer holds. Drop it rather than letting a count go negative
            # (SPEC.md §6.2 requires non-negative counts) or fabricating an
            # ungrounded arrival.
            del self._pending[pending.connector_id]
            return

        if source != OUTSIDE:
            self._counts[source] -= 1
            self._last_confirmed[source] = now
            self._last_provenance[source] = provenance
        self._counts[pending.dest_area_id] += 1
        self._last_confirmed[pending.dest_area_id] = now
        self._last_provenance[pending.dest_area_id] = provenance
        del self._pending[pending.connector_id]
